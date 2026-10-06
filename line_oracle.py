"""line_oracle.py — Oracolo a LINEA per OU/AH, follow-the-money (30/09/2026).

PERCHE' ESISTE. I pick OU/AH morivano tutti con `no_oracle`: il gate top-down
(`auto_bet._top_down_eval`) legge `probs.get(pick["esito_key"])` dove l'esito
e' 'Over 2.5' / 'Home -0.75' — chiavi che un oracolo 1X2 NON ha mai. La
p_true a linea richiede i mercati `totals,spreads` di Pinnacle, e the-odds-api
addebita `markets x regions` per chiamata: metterli sulla rotazione di ricerca
triplicherebbe OGNI chiamata (~938 crediti/mese, fuori dal tetto 460 anche
tagliando la rotazione).

Il design scelto (direttiva "tagliare la rotazione quote" interpretata come
budget: si taglia il costo pagando SOLO dove c'e' denaro in gioco) e'
FOLLOW-THE-MONEY:
- la rotazione di ricerca resta `markets="h2h"` (1 credito) — INVARIATA;
- una seconda chiamata `markets="h2h,totals,spreads"` (3 crediti) viene fatta
  SOLO per le leghe con pick OU/AH aperti in finestra d'ordine;
- cache separata `toao_<sport>.json`, con la TTL ALLINEATA alla finestra di
  fetch (`odds_api.oracle_cache_ttl_s()`), budget giornaliero dedicato
  (`ORACLE_BUDGET_DAY`) e hard-stop crediti rispettati.

FINESTRA DI FETCH (03/10/2026, direttiva del proprietario; 120 minuti dal
05/10/2026). Si ordina SOLO nella finestra esecutiva T-180..T-2: query e
selezione usano la STESSA finestra (`odds_api.ORACLE_FETCH_WINDOW_MIN`, default
**120 minuti**), quindi si scarica e si parsa solo cio' che puo' diventare un
ordine — non l'intero palinsesto della lega (era 24h). ⚠️ Il costo the-odds-api
e' per CHIAMATA, non per evento: restringere la finestra non riduce i crediti,
riduce il payload. I 120 minuti sono anche cio' che rende PAGABILE il primo
checkpoint di refetch (T-120'): con 70' la fetch veniva rifiutata a monte.

Costo atteso misurato: ~118 crediti/mese con la finestra larga; con la
finestra stretta il tetto resta `ORACLE_BUDGET_DAY` x 3 crediti/giorno
(in produzione 2 x 3 = 6). La DIAGONALE `line_true_probs` (e' in
`pinnacle_oracle`) resta sempre a costo ZERO: legge solo le cache.

GARANZIE (tripwire in `test_line_oracle.py`):
- sola orchestrazione: nessuna scrittura sul ledger, nessun ordine, nessuna
  formula di de-vig (la matematica sta in `pinnacle_oracle`/`market_calib`);
- fail-safe: qualunque errore per lega e' contato in `errors`, mai propagato
  (un problema dell'oracolo non ferma il bot);
- `HTTP` passa solo da `odds_api.fetch_line_odds` (che porta budget, hard-stop
  e should_query_sport): nessuna chiamata diretta a the-odds-api da qui.

CLI: venv/bin/python line_oracle.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Quante leghe fetchare per giro (doppio tetto col budget giornaliero di
# `odds_api.ORACLE_BUDGET_DAY`): i pick piu' vicini al kickoff prima.
LEAGUES_PER_PASS = int(os.getenv("ORACLE_LEAGUES_PER_PASS", "3"))


def _window_h() -> float:
    """Finestra dell'oracolo a linea, in ORE, dalla STESSA env del fetch.

    UNA sola definizione (03/10/2026, direttiva del proprietario): si ordina
    solo nella finestra esecutiva T-60..T-5, quindi non ha senso pagare (ne'
    scaricare) le partite che entreranno in finestra fra mezza giornata.
    Delegare a `odds_api.oracle_fetch_window_min()` (default 120 minuti, env
    `ORACLE_FETCH_WINDOW_MIN`) impedisce che selezione dei pick e query HTTP
    usino orizzonti diversi: pagheremmo leghe le cui partite non entrano
    nell'intervallo scaricato (e salteremmo leghe che hanno pick in finestra).
    """
    try:
        import odds_api as oa
        return oa.oracle_fetch_window_min() / 60.0
    except Exception:                                            # pragma: no cover
        return 120 / 60.0


def budget_credits_per_day() -> float:
    """Costo MAX/dell'oracolo a linea in crediti (tetto, non target).

    = leghe/giorno ammesse dal budget `odds_api.ORACLE_BUDGET_DAY` x 3
    crediti a chiamata (`h2h,totals,spreads`). Il test di budget usa questo
    numero: il consumo REALE e' piu' basso (segue i pick in gioco).
    """
    try:
        import odds_api as oa
        budget = int(oa.ORACLE_BUDGET_DAY)
        credits = 1 + int(getattr(oa, "ORACLE_EXTRA_CREDITS", 2))
    except Exception:                                        # pragma: no cover
        budget, credits = 12, 3
    return float(max(0, budget) * credits)


def _parse_iso(ts: Any) -> Optional[datetime]:
    """Kickoff del ledger ('T'/'Z'/spazio) -> datetime UTC. None se ignoto."""
    try:
        s = str(ts or "").strip().replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def line_picks() -> List[Dict[str, Any]]:
    """Pick OU/AH aperti in finestra, con lega risolta in sport key.

    Riusa ESATTAMENTE la selezione della corsia d'ordine
    (`multi_market.live_picks`: lega ammessa, fascia quota, lato favorito,
    esito riconoscibile) cosi' il budget segue il DENARO, non la telemetria.
    La lega viene convertita con `sx_signals.league_to_sport` (l'UNICA mappa
    SX/the-odds-api del progetto). Ordinati per kickoff crescente (prima le
    partite piu' vicine: sono quelle che entreranno in finestra T-60).
    """
    try:
        import multi_market
        picks = multi_market.live_picks(hours=_window_h())
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: corsia multi-mercato non disponibile (%s)",
                       exc)
        return []
    try:
        from sx_signals import league_to_sport
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: league_to_sport non disponibile (%s)", exc)
        return []
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=_window_h())
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for p in picks:
        k = (p.get("match_id"), p.get("esito_key"))
        if k in seen:
            continue
        seen.add(k)
        ko = _parse_iso(p.get("commence"))
        if ko is None or ko > deadline:
            continue
        sport = league_to_sport(p.get("league") or "")
        if not sport:
            continue
        out.append({"match_id": p.get("match_id"), "esito_key": p.get("esito_key"),
                    "league": p.get("league"), "sport_key": sport,
                    "kickoff": ko.isoformat(),
                    "kickoff_ts": ko.timestamp()})
    out.sort(key=lambda x: x["kickoff_ts"])
    return out


def is_core_league(league: Any) -> bool:
    """True SOLO per le leghe Tier-1/Core — regola dei fetch a PAGAMENTO.

    Direttiva "League Tiering" (05/10/2026): il refetch a pagamento e' riservato
    alle leghe Core; le leghe in probation si valutano SOLO sulla cache passiva.
    La definizione vive in `value_filter.is_core_league` (un solo insieme di
    leghe nel progetto) e qui si consuma — mai copiata.

    FAIL-CLOSED: se il tier non e' leggibile (import rotto) si risponde False,
    cioe' NON si spende. Una guardia di spesa non si apre per un errore.
    """
    name = str(league or "").strip()
    if not name:
        return False
    try:
        from value_filter import is_core_league as _vf_is_core
        return bool(_vf_is_core(name))
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: tier di lega non leggibile (%s): "
                       "nessun fetch a pagamento", exc)
        return False


def _league_plan(now: Optional[float] = None
                 ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Piano di fetch: `(leghe da pagare, leghe escluse per tier)`.

    Estratto da `leagues_needing_fetch` il 06/10/2026 perche' la stessa
    classificazione serve a DUE lettori: chi paga (che vede solo le leghe Core)
    e la telemetria (che deve poter DICHIARARE quante leghe sono state escluse
    per tier: un'esclusione non misurata e' indistinguibile da un'assenza di
    pick).

    La cache `toao_<sport>.json` copre la FINESTRA (`oracle_fetch_window_min()`
    minuti): se e' fresca (eta' < TTL dinamica) la lega non serve (gia' pagata).
    """
    try:
        import odds_api as oa
        from config import DATA_DIR
        from pathlib import Path
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: odds_api non disponibile (%s)", exc)
        return []
    ts_now = time.time() if now is None else float(now)
    # Finestra "in gioco": un pick oltre l'orizzonte non genera ordini oggi
    # (la sua linea si paga quando si avvicina il kickoff).
    deadline = ts_now + _window_h() * 3600.0
    per_sport: Dict[str, Dict[str, Any]] = {}
    blocked: Dict[str, Dict[str, Any]] = {}
    for p in line_picks():
        if float(p.get("kickoff_ts") or 0) > deadline:
            continue
        # LEAGUE TIERING (06/10/2026). Il filtro mancava QUI: lo scheduler
        # (`bot.line_oracle_job` -> `ensure_oracle_payloads`) pagava 3 crediti
        # anche per le leghe in probation, mentre il percorso on-demand
        # (`auto_bet._ondemand_fetch`) e l'harvesting li filtravano gia'. Ecco
        # il difetto misurato il 05/10: `soccer_argentina_primera_division`
        # (Tier-2) fetchata ~ogni 30' per un totale di ~39 crediti in un giorno
        # con `ORACLE_BUDGET_DAY=2`. La lega resta nel ledger e nella
        # valutazione: cambia solo CHI paga.
        if not is_core_league(p.get("league")):
            b = blocked.setdefault(p["sport_key"],
                                   {"sport_key": p["sport_key"],
                                    "league": p.get("league"), "picks": 0})
            b["picks"] += 1
            continue
        sp = p["sport_key"]
        if sp not in per_sport:
            per_sport[sp] = {"sport_key": sp, "league": p.get("league"),
                             "picks": 0, "min_kickoff": p["kickoff_ts"]}
        per_sport[sp]["picks"] += 1
        per_sport[sp]["min_kickoff"] = min(per_sport[sp]["min_kickoff"],
                                           p["kickoff_ts"])
    out: List[Dict[str, Any]] = []
    for sp, info in per_sport.items():
        # TTL DINAMICO (05/10/2026, direttiva del proprietario): piu' il kickoff
        # e' vicino, piu' corta e' la vita utile del dato — 30 min oltre T-180,
        # 5 min nella finestra T-60..T-180, 2 min sotto T-60. La formula vive in
        # `pinnacle_oracle.cache_ttl_minutes` (UNICA definizione: la STESSA che
        # applica il GATE in `_oracle_fixture_status`). Se qui si usasse la TTL
        # di finestra (70 min) il refresh chiesto dallo scheduler sarebbe un
        # cache-hit (nessuna spesa, nessun aggiornamento) e il gate avrebbe
        # continuato a scartare per cache scaduta.
        #
        # ⚠️ Il refresh CONSUMA il budget `ORACLE_BUDGET_DAY` (3 crediti/lega):
        # le leghe sono ordinate per kickoff CRESCENTE e `ensure_oracle_payloads`
        # taglia a `LEAGUES_PER_PASS`, quindi il budget va alla partita piu'
        # vicina (quella che sta per entrare in finestra esecutiva). A budget
        # esaurito il gate resta fail-closed e il pick e' saltato: onesto, non
        # silenzioso.
        try:
            import pinnacle_oracle as po
            ttl_s = float(po.cache_ttl_minutes(
                po.minutes_to_kickoff(info["min_kickoff"], now=ts_now)) * 60.0)
        except Exception:                                    # pragma: no cover
            ttl_s = float(oa.oracle_cache_ttl_s())
        info["ttl_min"] = round(ttl_s / 60.0, 1)
        info["ttl_s"] = ttl_s
        cache = Path(DATA_DIR) / f"{oa.ORACLE_CACHE_PREFIX}{sp}.json"
        age = None
        try:
            if cache.exists():
                data = json.loads(cache.read_text())
                age = (ts_now - float(data.get("ts") or 0))
                if age < ttl_s:
                    continue        # cache fresca: la lega e' gia' coperta
        except Exception:
            age = None              # cache corrotta = da rifare
        info["cache_age_h"] = round(age / 3600.0, 1) if age is not None else None
        out.append(info)
    out.sort(key=lambda x: x["min_kickoff"])
    tier_blocked = sorted(blocked.values(), key=lambda x: -x["picks"])
    return out, tier_blocked


def leagues_needing_fetch(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Sport key DISTINTI con pick a linea **Core** in gioco e cache stantia.

    Le leghe in probation NON sono qui: si valutano solo sulla cache passiva
    (direttiva League Tiering). Restano leggibili con `leagues_blocked_by_tier`.
    """
    return _league_plan(now)[0]


def leagues_blocked_by_tier(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Leghe con pick a linea in gioco ESCLUSE dal fetch a pagamento (probation).

    Telemetria, non decisione: serve a distinguere "nessun pick a linea" da
    "pick presenti ma lega non Tier-1/Core" (la domanda del 06/10: quante
    occasioni restano sulla cache passiva per scelta di tier?).
    """
    return _league_plan(now)[1]


def ensure_oracle_payloads(max_leagues: Optional[int] = None
                           ) -> Dict[str, Any]:
    """Fetch `h2h,totals,spreads` per le leghe che ne hanno bisogno.

    Costo: 3 crediti per lega fetchata (via `odds_api.fetch_line_odds`, che
    porta budget giornaliero e hard-stop). Ritorna un riepilogo DICHIARATO:
    `leagues` (richieste), `fetched`/`skipped`/`errors`, `requests_today`.
    """
    try:
        import odds_api as oa
    except Exception as exc:                                     # pragma: no cover
        return {"leagues": [], "fetched": 0, "skipped": 0, "errors": 1,
                "error": str(exc), "requests_today": 0}
    cap = LEAGUES_PER_PASS if max_leagues is None else int(max_leagues)
    plan, tier_blocked = _league_plan()
    pending = plan[:max(0, cap)]
    res: Dict[str, Any] = {"leagues": [x["sport_key"] for x in pending],
                           "fetched": 0, "skipped": 0, "errors": 0,
                           "rows": [], "tier_excluded": tier_blocked,
                           "requests_today":
                           getattr(oa, "_oracle_req_day", {}).get("n", 0)}
    if tier_blocked:
        logger.info("oracolo a linea: %d leghe in gioco, %d escluse dal fetch "
                    "a pagamento (non Tier-1/Core: valutazione solo sulla "
                    "cache passiva) — %s", len(pending), len(tier_blocked),
                    ", ".join(f"{b['league']} ({b['picks']})"
                              for b in tier_blocked[:4]))
    now = datetime.now(timezone.utc)
    frm = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (now + timedelta(hours=_window_h())).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for info in pending:
        sp = info["sport_key"]
        try:
            payload, remaining = oa.fetch_line_odds(sp, frm, to,
                                                    ttl_s=info.get("ttl_s"))
        except Exception as exc:
            res["errors"] += 1
            res["rows"].append({"sport": sp, "error": str(exc)})
            continue
        if payload:
            res["fetched"] += 1
            res["rows"].append({"sport": sp, "matches": len(payload),
                                "remaining": remaining})
        else:
            res["skipped"] += 1
            res["rows"].append({"sport": sp, "skipped": True,
                                "remaining": remaining})
    res["requests_today"] = getattr(oa, "_oracle_req_day", {}).get("n", 0)
    return res


# ---------------------------------------------------------------------------
# FETCH ON-DEMAND (05/10/2026) — il budget segue il PICK, non il calendario
# ---------------------------------------------------------------------------
# PERCHE'. Lo scheduler fetcha ogni 30' scegliendo per "kickoff piu' vicino":
# il budget giornaliero (`odds_api.ORACLE_BUDGET_DAY`, CONDIVISO) puo' cosi'
# finire su una lega i cui pick non sono ordinabili (finestra T-180..T-2 non
# ancora aperta) mentre il pick che sta per diventare un ordine resta con la
# cache scaduta per il TTL dinamico -> `no_oracle/EXPIRED_CACHE` per sempre
# (misurato in produzione il 05/10/2026: 11 pick/ciclo, tutti scaduti a 17'
# con TTL 2').
#
# COME. Il gate, quando incontra `EXPIRED_CACHE` su un pick IN FINESTRA,
# paga SUBITO la fetch della SUA lega (3 crediti) e salta il pick: al giro
# successivo (60s) la cache e' fresca e la valutazione passa. Nessun credito
# in piu' del tetto giornaliero: entrambi i percorsi passano per
# `odds_api.fetch_line_odds`, che porta budget, hard-stop e `ORACLE_ENABLED`.
#
# 05/10/2026 — DUE FRENI AGGIUNTI (direttiva del proprietario):
# 1. LEAGUE TIERING: il refetch a PAGAMENTO e' riservato alle leghe Tier-1/Core
#    (`auto_bet._ondemand_fetch`, che e' l'unico chiamante). Le leghe in
#    probation si valutano SOLO sulla cache passiva: 3 crediti non si spendono
#    su un campionato di cui non e' ancora stato misurato un ROI positivo.
# 2. CHECKPOINT su `MISSING_MARKET` (qui sotto): un mercato non pubblicato si
#    richiede a T-120' e a T-70', non a ogni ciclo di 60s.
_ONDEMAND_DEFAULT_DEDUP_S = 120.0
_last_ondemand: Dict[str, float] = {}


def ondemand_dedup_s() -> float:
    """Secondi minimi fra due fetch on-demand della STESSA lega.

    Env `ORACLE_ONDEMAND_DEDUP_S` (default 120s). Una tornata di 12 pick sulla
    stessa lega paga UNA volta: senza dedup il gate brucerebbe l'intero budget
    giornaliero in una manciata di secondi. Un valore assente, non numerico o
    non positivo ricade sul default (una guardia non si spegne con un env
    sbagliato).
    """
    raw = os.getenv("ORACLE_ONDEMAND_DEDUP_S")
    if raw in (None, ""):
        return _ONDEMAND_DEFAULT_DEDUP_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("line_oracle: ORACLE_ONDEMAND_DEDUP_S=%r non numerico, "
                       "uso %s", raw, _ONDEMAND_DEFAULT_DEDUP_S)
        return _ONDEMAND_DEFAULT_DEDUP_S
    if val <= 0:
        logger.warning("line_oracle: ORACLE_ONDEMAND_DEDUP_S=%r non positivo, "
                       "uso %s", raw, _ONDEMAND_DEFAULT_DEDUP_S)
        return _ONDEMAND_DEFAULT_DEDUP_S
    return val


def reset_ondemand_dedup() -> None:
    """Azzera la memo in-process (test e diagnostica: nessun altro uso)."""
    _last_ondemand.clear()


def ondemand_enabled() -> bool:
    """Interruttore del fetch on-demand (env `ORACLE_ONDEMAND_ENABLED`).

    Default ATTIVO (una funzione nuova non si spegne da sola su un deploy);
    per disattivarla serve un valore esplicito fra `0/false/no/off/disabled`.
    Spenta, il gate resta fail-closed come prima (nessuna spesa): e' cio' che
    serve a test e diagnostiche che esercitano il percorso reale senza toccare
    la rete (`verify_guardrails.py` lo imposta a 0 insieme a `LIVE_INTEL=0` e
    `ESPORTS_LIVE=0`).
    """
    raw = (os.getenv("ORACLE_ONDEMAND_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "disabled")


# ---------------------------------------------------------------------------
# CHECKPOINT DI REFETCH per MISSING_MARKET (05/10/2026, direttiva del proprietario)
# ---------------------------------------------------------------------------
# Un mercato che Pinnacle non ha pubblicato (MISSING_MARKET) non manca perche'
# il dato sia scaduto: manca perche' lo si e' chiesto troppo presto. Senza un
# freno il gate lo ri-chiede a OGNI ciclo di 60s (fino a 1440 richieste al
# giorno per la stessa partita) bruciando il budget su un mercato che potrebbe
# non arrivare mai. Due soli checkpoint, T-120' e T-70': quando la partita
# scende sotto le 2 ore UNA richiesta, quando scende sotto i 70 minuti UNA
# seconda. Stato PERSISTENTE sul volume: un redeploy non riapre la spesa.
_CHECKPOINT_T120_MIN = 120.0
_CHECKPOINT_T70_MIN = 70.0
_CHECKPOINT_MAX_AGE_S = 7 * 24 * 3600.0
_CHECKPOINT_MEMO: Dict[str, Any] = {"state": None, "path": None}


def checkpoint_state_path():
    """Path dello stato dei checkpoint (env `ORACLE_CHECKPOINT_STATE`).

    Letto a RUNTIME (non all'import): i test e le diagnostiche lo spostano
    senza toccare il volume di produzione.
    """
    from pathlib import Path
    raw = (os.getenv("ORACLE_CHECKPOINT_STATE") or "").strip()
    if raw:
        return Path(raw)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR) / "decision" / "oracle_checkpoints.json"
    except Exception:                                            # pragma: no cover
        return Path("data") / "decision" / "oracle_checkpoints.json"


def _read_checkpoint_file(path) -> Dict[str, Any]:
    """Stato dal volume: `{}` se assente o illeggibile (con un warning).

    Un file corrotto NON e' un motivo per pagare: la memo in-process continua
    a valere, quindi il caso peggiore e' una richiesta per checkpoint per
    processo (mai un ciclo di 60s che paga).
    """
    try:
        from pathlib import Path
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("line_oracle: stato checkpoint illeggibile (%s)", exc)
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("line_oracle: stato checkpoint non JSON (%s)", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            label = str(val.get("checkpoint") or "")
            ts = val.get("ts")
        else:
            label, ts = str(val or ""), None
        if label:
            out[str(key)] = (label, ts)
    return out


def _checkpoint_memo() -> Dict[str, Any]:
    """Memo in-process, ricaricata se il PATH e' cambiato (isolamento test)."""
    path = str(checkpoint_state_path())
    if _CHECKPOINT_MEMO["state"] is None or _CHECKPOINT_MEMO["path"] != path:
        _CHECKPOINT_MEMO["path"] = path
        _CHECKPOINT_MEMO["state"] = _read_checkpoint_file(path)
    return _CHECKPOINT_MEMO["state"]


def _write_checkpoint_file(path, state: Dict[str, Any]) -> None:
    """Scrittura ATOMICA (tmp + os.replace); un errore non propaga."""
    try:
        from pathlib import Path
        now = time.time()
        payload = {k: {"checkpoint": v[0], "ts": v[1]}
                   for k, v in state.items()
                   if isinstance(v, (tuple, list)) and len(v) >= 1
                   and (v[1] is None or now - float(v[1]) <= _CHECKPOINT_MAX_AGE_S)}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:
        logger.debug("line_oracle: stato checkpoint non scritto (%s)", exc)


def checkpoint_for(minutes_to_kickoff: Optional[float]) -> Optional[str]:
    """Checkpoint di refetch aperto ADESSO (None = troppo presto).

    `T-70` sotto i 70 minuti, `T-120` fino a 2 ore, `None` oltre: la prima
    richiesta ammessa e' quella del checkpoint T-120 (per questo la finestra
    di fetch deve arrivare a 120', `odds_api.ORACLE_FETCH_WINDOW_MIN`).
    """
    try:
        mtk = float(minutes_to_kickoff)                          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if mtk <= 0 or mtk > _CHECKPOINT_T120_MIN:
        return None
    return "T-70" if mtk <= _CHECKPOINT_T70_MIN else "T-120"


def checkpoint_honoured(match_id: Any) -> Optional[str]:
    """Checkpoint gia' onorato per quella partita (None = nessuno)."""
    key = str(match_id or "").strip()
    if not key:
        return None
    entry = _checkpoint_memo().get(key)
    if isinstance(entry, (tuple, list)) and entry:
        return str(entry[0])
    return str(entry) if isinstance(entry, str) else None


def mark_checkpoint(match_id: Any, label: Optional[str]) -> None:
    """Segna il checkpoint come onorato (partita -> etichetta)."""
    key = str(match_id or "").strip()
    if not key or not label:
        return
    state = _checkpoint_memo()
    state[key] = (str(label), time.time())
    _write_checkpoint_file(checkpoint_state_path(), state)


def reset_checkpoints() -> None:
    """Azzera la memo in-process (test e diagnostica: nessun altro uso)."""
    _CHECKPOINT_MEMO["state"] = None
    _CHECKPOINT_MEMO["path"] = None


def fetch_for_pick(pick: Dict[str, Any],
                   now: Optional[float] = None,
                   code: Optional[str] = None) -> Dict[str, Any]:
    """Paga ORA la fetch `h2h,totals,spreads` della lega di QUESTO pick.

    Ritorna SEMPRE un dict con `fetched` (bool) e `reason` machine-readable:
    `fetch on-demand eseguita` | `dedup (...)` | `lega non mappata` |
    `budget oracolo esaurito` | `hard-stop crediti` | `errore fetch (...)` |
    `nessun payload (nessuna partita in finestra)` | `checkpoint ...`.

    Tetti (nessuno aggirabile da qui): budget giornaliero, hard-stop crediti,
    `ORACLE_ENABLED` e `should_query_sport` vivono in `odds_api.fetch_line_odds`
    (l'unico punto HTTP autorizzato del progetto). La dedup per lega e' locale
    al processo; il chiamante verifica la FINESTRA ESECUTIVA prima di invocare.

    `code` e' la DIAGNOSI del gate (`EXPIRED_CACHE`/`MISSING_MARKET`/...). Con
    `MISSING_MARKET` vale la regola dei CHECKPOINT (T-120'/T-70', 05/10/2026):
    un mercato non pubblicato si richiede due volte, non a ogni ciclo.

    Fail-safe: mai un'eccezione (un problema dell'oracolo non ferma il bot).
    """
    if not ondemand_enabled():
        return {"fetched": False, "sport_key": None,
                "reason": "fetch on-demand disattivata (ORACLE_ONDEMAND_ENABLED)"}
    sport = None
    try:
        import odds_api as oa
        from sx_signals import league_to_sport
    except Exception as exc:                                     # pragma: no cover
        return {"fetched": False, "reason": f"dipendenze non disponibili ({exc})",
                "sport_key": None}
    sport = league_to_sport(str(pick.get("league") or ""))
    if not sport:
        return {"fetched": False, "reason": "lega non mappata a uno sport key",
                "sport_key": None}
    ts_now = time.time() if now is None else float(now)
    # FINESTRA DEL PAYLOAD: la query scarica `now .. now + ORACLE_FETCH_WINDOW_MIN`
    # (default 120'), quindi una partita a T-170 non entrerebbe nel payload: la
    # fetch sarebbe 3 crediti buttati e il pick resterebbe senza p_true. Si
    # paga SOLO se il kickoff e' dentro quella finestra (05/10/2026).
    window_min = _window_h() * 60.0
    mtk: Optional[float] = None
    try:
        import pinnacle_oracle as po
        mtk = po.minutes_to_kickoff(pick.get("commence") or pick.get("kickoff"),
                                    now=ts_now)
    except Exception:                                            # pragma: no cover
        mtk = None
    if mtk is None:
        return {"fetched": False, "sport_key": sport,
                "reason": "kickoff ignoto: nessuna fetch (fail-closed)"}
    if mtk < 0:
        # Partita gia' iniziata: la finestra esecutiva la esclude a monte
        # (`pick_window`), ma una funzione che spende non si fida del
        # chiamante (fail-closed, motivo dichiarato).
        return {"fetched": False, "sport_key": sport,
                "reason": f"kickoff gia' passato ({mtk:.0f} min): nessuna fetch"}
    if mtk > window_min:
        return {"fetched": False, "sport_key": sport,
                "reason": (f"kickoff oltre la finestra di fetch "
                           f"({mtk:.0f}' > {window_min:.0f}')")}
    # CHECKPOINT di refetch per MISSING_MARKET (05/10/2026, direttiva del
    # proprietario). Un mercato che Pinnacle non pubblica non manca perche' il
    # dato e' scaduto: manca perche' lo si e' chiesto troppo presto. Senza
    # freno il gate lo ri-chiede a OGNI ciclo di 60s (fino a 1440 richieste al
    # giorno sulla stessa partita) bruciando il budget su un mercato che
    # potrebbe non arrivare mai. Due soli tentativi per partita, a T-120' e a
    # T-70'; per le cause DIVERSE (es. `EXPIRED_CACHE`) il refresh resta
    # libero: li' il dato esiste e va solo rinfrescato, e la frequenza la
    # limitano gia' la dedup per lega e la TTL dinamica del pick.
    label: Optional[str] = None
    if code == "MISSING_MARKET":
        label = checkpoint_for(mtk)
        if label is None:
            return {"fetched": False, "sport_key": sport,
                    "reason": (f"checkpoint non aperto ({mtk:.0f}'): un "
                               f"mercato mancante si richiede a T-120' e "
                               f"T-70'")}
        honoured = checkpoint_honoured(pick.get("match_id") or pick.get("id"))
        if honoured == label:
            return {"fetched": False, "sport_key": sport,
                    "reason": f"checkpoint {label} gia' onorato"}
    dedup = ondemand_dedup_s()
    last = _last_ondemand.get(sport)
    if last is not None and (ts_now - last) < dedup:
        return {"fetched": False,
                "reason": f"dedup ({ts_now - last:.0f}s < {dedup:.0f}s)",
                "sport_key": sport}
    # TTL dinamico del PICK (stessa formula del gate): il payload appena
    # scaricato deve risultare FRESCO al prossimo giro, altrimenti la fetch
    # sarebbe spesa per un dato che il gate scarterebbe di nuovo.
    ttl_s: Optional[float] = None
    try:
        ttl_s = float(po.cache_ttl_minutes(mtk) * 60.0)
    except Exception:                                            # pragma: no cover
        ttl_s = None
    now_dt = datetime.fromtimestamp(ts_now, tz=timezone.utc)
    frm = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (now_dt + timedelta(hours=_window_h())).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Override LOCALE della sola dedup: `fetch_line_odds` ha il suo
    # `cache_min_age_s` (protegge dalla ri-scrittura della cache) e non deve
    # essere confuso con la dedup per lega del budget on-demand.
    # La memo si aggiorna PRIMA della chiamata: un errore di rete non deve
    # produrre un nuovo tentativo a ogni pick dello stesso giro.
    _last_ondemand[sport] = ts_now
    try:
        payload, remaining = oa.fetch_line_odds(sport, frm, to, ttl_s=ttl_s)
    except Exception as exc:
        # Nessun checkpoint consumato: non sappiamo se la richiesta e' partita
        # (un errore di rete e' transitorio, un tentativo speso no).
        return {"fetched": False, "reason": f"errore fetch ({exc})",
                "sport_key": sport}
    # Il tentativo e' avvenuto: il checkpoint si consuma ANCHE se il payload e'
    # tornato vuoto (budget finito o nessuna partita nella finestra). La regola
    # e' "due tentativi", non "due riusciti": un payload vuoto non e' una
    # ragione per riprovare fra 60 secondi.
    if label:
        mark_checkpoint(pick.get("match_id") or pick.get("id"), label)
    if payload:
        return {"fetched": True, "reason": "fetch on-demand eseguita",
                "sport_key": sport, "matches": len(payload),
                "remaining": remaining, "checkpoint": label}
    return {"fetched": False, "reason": _no_payload_reason(),
            "sport_key": sport, "remaining": remaining,
            "checkpoint": label}


def _no_payload_reason() -> str:
    """PERCHE' la fetch non ha prodotto payload (causa DICHIARATA, non 'vuoto').

    Ordine di lettura: hard-stop crediti -> budget oracolo del giorno ->
    cache non ri-scritta / nessuna partita nella finestra. Non inventa una
    causa che non puo' verificare: se nessuna sonda risponde, resta il
    generico (onesto).
    """
    try:
        import odds_api as oa
        try:
            if oa.credits_hard_stopped():                        # pragma: no cover
                return "hard-stop crediti"
        except Exception:
            pass
        try:
            used = int(getattr(oa, "_oracle_req_day", {}).get("n") or 0)
            cap = int(getattr(oa, "ORACLE_BUDGET_DAY", 0) or 0)
            if cap > 0 and used >= cap:
                return f"budget oracolo esaurito ({used}/{cap} oggi)"
        except Exception:
            pass
        try:
            if not bool(oa.ORACLE_ENABLED):
                return "ORACLE_ENABLED=0"
        except Exception:
            pass
    except Exception:                                            # pragma: no cover
        pass
    return "nessun payload (nessuna partita in finestra o cache non riscritta)"


def format_report(res: Dict[str, Any]) -> str:
    lines = ["🎯 Oracolo a linea OU/AH (follow-the-money)"]
    budget = budget_credits_per_day()
    excluded = res.get("tier_excluded") or []
    lines.append(f"  tetto giornaliero: {budget:g} crediti ({len(excluded)} "
                 f"leghe in gioco fuori dal perimetro Tier-1/Core)")
    if excluded:
        lines.append("  escluse per tier (cache passiva): " + ", ".join(
            f"{b.get('league')} ({b.get('picks')})" for b in excluded[:5]))
    leagues = res.get("leagues") or []
    if not leagues:
        lines.append("  nessuna lega da fetchare (nessun pick a linea Core in "
                     "gioco o cache fresche)")
        return "\n".join(lines)
    lines.append(f"  leghe richieste: {len(leagues)} | fetch {res.get('fetched', 0)}"
                 f" | skip {res.get('skipped', 0)} | errori {res.get('errors', 0)}"
                 f" | richieste oggi {res.get('requests_today', 0)}")
    for r in res.get("rows") or []:
        if r.get("error"):
            lines.append(f"  ⚠️ {r.get('sport')}: errore {r.get('error')}")
        elif r.get("skipped"):
            lines.append(f"  • {r.get('sport')}: skip (remaining "
                         f"{r.get('remaining')})")
        else:
            lines.append(f"  ✅ {r.get('sport')}: {r.get('matches')} match "
                         f"(crediti {r.get('remaining')})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:               # pragma: no cover
    ap = argparse.ArgumentParser(description="Oracolo a linea OU/AH")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra le leghe che verrebbero fetchate (no HTTP)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        pending = leagues_needing_fetch()
        print(json.dumps(pending, indent=2, ensure_ascii=False))
        return 0
    res = ensure_oracle_payloads()
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
