"""
API JSON (stdlib, nessuna dipendenza) che espone i dati del bot QuotaVerace
ai frontend (webapp Next.js su Vercel).

Endpoint:
  GET /api/health            -> stato + crediti the-odds-api
  GET /api/dashboard         -> KPIs (bankroll, ROI, segnali oggi) + ultime value bet
  GET /api/storico           -> storico segnali + riepilogo 30gg
  GET /api/value             -> migliori value bet filtrate
  GET /api/schedina          -> schedina del giorno (picks + multipla)
  GET /api/scan              -> rimosso (04/09): Betfair non è più in architettura
  GET /api/drift             -> stato drift modello (verifica remota)
  GET /api/calibration       -> dashboard calibrazione (Brier/ECE, curve)
  GET /api/significance      -> significativita' statistica del campione
  GET /api/agents            -> tabella di comando: stato di OGNI agente,
                                sicurezza, capitale e flusso (sola lettura)

Uso su Railway: dopo aver deployato il bot, crea un secondo servizio con
  startCommand = "python web_api.py"
oppure avvialo a fianco del bot. Porta di default 8000 (override via WEB_API_PORT).
"""

import hmac
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import DATA_DIR
from tracker import _get_conn, _create_results_table, get_performance_summary, get_signals

logger = logging.getLogger("web_api")

# Railway injects PORT; fall back to WEB_API_PORT, then 8000 for local use.
PORT = int(os.getenv("PORT") or os.getenv("WEB_API_PORT") or "8000")
DB = DATA_DIR / "quotaverace.db"

# --- R1 (19/10/2026): protezione delle rotte che possono toccare il denaro ---
# Fail-closed a strati: senza API_ADMIN_TOKEN (>= 24 char) la rotta e' 503;
# con il token ma senza API_AUTO_BET_ENABLED=1 e' 403; senza
# API_AUTO_BET_ALLOW_LIVE=1 il giro e' FORZATO in SIM. Un token rubato, da
# solo, non basta a piazzare un ordine reale.
# NB: il NOME della variabile (non un valore) — il tripwire di igiene
# segreti (test_secret_hygiene) segnala le assegnazioni con nomi
# "credential-like": qui il valore e' una chiave d'ambiente, non un
# segreto, e il nome della costante lo dichiara.
ADMIN_ENV_NAME = "API_ADMIN_TOKEN"
MIN_ADMIN_TOKEN_LEN = 24
AUTO_BET_ENABLED_ENV = "API_AUTO_BET_ENABLED"
AUTO_BET_ALLOW_LIVE_ENV = "API_AUTO_BET_ALLOW_LIVE"
# P1 (19/10/2026): il permesso e' per METODO, non per path. `GET /api/cassa`
# e' una lettura e resta pubblica; `POST`/`DELETE /api/cassa` scrivono il
# ledger della cassa e richiedono il token admin. Proteggere il path a
# prescindere dal metodo renderebbe illeggibile la pagina Cassa del sito.
ADMIN_METHOD_ROUTES = {
    "POST": frozenset({"/api/auto_bet", "/api/analisi", "/api/cassa"}),
    "DELETE": frozenset({"/api/cassa"}),
}
# Unione dei path (compatibilita' con R1 e per i report diagnostici).
ADMIN_ROUTES = frozenset().union(*ADMIN_METHOD_ROUTES.values())

# Secondo interruttore esplicito per le scritture P1 (analisi/cassa): il token
# dice CHI, questo dice SE. Stessa logica di API_AUTO_BET_ENABLED.
WRITE_ENABLED_ENV = "API_WRITE_ENABLED"

# Separazione lettura/admin: se API_READ_TOKEN e' configurato le GET lo
# richiedono (o accettano il token admin); se non lo e' restano pubbliche
# (il sito legge la dashboard). Con API_REQUIRE_READ_TOKEN=1 la modalita'
# diventa fail-closed: senza token di lettura le GET rispondono 503.
READ_ENV_NAME = "API_READ_TOKEN"
READ_STRICT_ENV = "API_REQUIRE_READ_TOKEN"
READ_PUBLIC_ROUTES = frozenset({"/api/health"})

# Conferma di cancellazione: una DELETE senza questa stringa non tocca il
# ledger. Protegge dall'errore (script, dito sbagliato), non dall'attaccante
# (quello lo ferma il token): il valore va scritto per intero.
CASSA_DELETE_CONFIRM = "CANCELLA_CASSA"

# Redazione dei segreti nei log della richiesta: `log_message` stampa la
# request line, quindi una chiave in query string finirebbe nel log. Il
# pattern copre i nomi usati dal progetto (key/token/api_key/password).
_SECRET_QUERY_RE = re.compile(
    r"((?:key|token|api_key|apikey|password)=)[^&\s\"']+", re.IGNORECASE)


def _env_flag(name: str, default: bool = False) -> bool:
    """True solo per 1/true/yes/on; variabile assente -> default.

    Un valore ignoto NON vale True: una protezione non si accende per caso.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _admin_token() -> str:
    return (os.getenv(ADMIN_ENV_NAME) or "").strip()


def _admin_header_token(headers) -> str:
    """Token dal header: `Authorization: Bearer ...` o `X-API-Token`.

    Mai dalla query string: finirebbe nei log del server e nella storia
    del browser (la stessa ragione per cui `key` non e' piu' su GET).
    """
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("X-API-Token") or "").strip()


def admin_check(headers) -> tuple[bool, int, dict]:
    """Autorizzazione delle rotte admin (pura, testabile senza rete).

    Tre esiti distinti, tutti fail-closed:
      503 admin_disabled        -> token assente (rotta spenta, non "aperta")
      503 admin_token_too_short -> token debole (non e' una protezione)
      401 unauthorized          -> token assente o errato
    Il confronto e' a tempo costante (`hmac.compare_digest`).
    """
    token = _admin_token()
    if not token:
        return False, 503, {
            "error": "admin_disabled",
            "message": f"{ADMIN_ENV_NAME} non configurato: rotta disattivata "
                       "(fail-closed).",
        }
    if len(token) < MIN_ADMIN_TOKEN_LEN:
        return False, 503, {
            "error": "admin_token_too_short",
            "message": f"{ADMIN_ENV_NAME} troppo corto (min "
                       f"{MIN_ADMIN_TOKEN_LEN} caratteri): rotta disattivata.",
        }
    supplied = _admin_header_token(headers)
    if not supplied:
        return False, 401, {
            "error": "unauthorized",
            "message": "Serve Authorization: Bearer <token> o X-API-Token.",
        }
    if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        return False, 401, {"error": "unauthorized", "message": "Token errato."}
    return True, 200, {}


def _read_token() -> str:
    return (os.getenv(READ_ENV_NAME) or "").strip()


def read_check(headers) -> tuple[bool, int, dict]:
    """Autorizzazione delle rotte di LETTURA (separata da quella admin).

    - API_READ_TOKEN assente: GET pubbliche (il sito legge i dati) salvo
      API_REQUIRE_READ_TOKEN=1, che le spegne (503 fail-closed).
    - API_READ_TOKEN presente: serve il token di lettura O quello admin.
    - Token di lettura troppo corto: 503 (non e' una protezione).
    """
    read = _read_token()
    if not read:
        if _env_flag(READ_STRICT_ENV, default=False):
            return False, 503, {
                "error": "read_disabled",
                "message": f"{READ_ENV_NAME} non configurato e "
                           f"{READ_STRICT_ENV}=1: letture disattivate.",
            }
        return True, 200, {}
    if len(read) < MIN_ADMIN_TOKEN_LEN:
        return False, 503, {
            "error": "read_token_too_short",
            "message": f"{READ_ENV_NAME} troppo corto (min "
                       f"{MIN_ADMIN_TOKEN_LEN} caratteri): letture disattivate.",
        }
    supplied = _admin_header_token(headers)
    if not supplied:
        return False, 401, {
            "error": "unauthorized",
            "message": "Serve Authorization: Bearer <token> o X-API-Token.",
        }
    admin = _admin_token()
    ok = hmac.compare_digest(supplied.encode("utf-8"), read.encode("utf-8"))
    if not ok and admin:
        ok = hmac.compare_digest(supplied.encode("utf-8"), admin.encode("utf-8"))
    if not ok:
        return False, 401, {"error": "unauthorized", "message": "Token errato."}
    return True, 200, {}


def _write_disabled() -> tuple[int, dict] | None:
    """Gate delle scritture P1: None = abilitate, altrimenti (codice, corpo)."""
    if _env_flag(WRITE_ENABLED_ENV, default=False):
        return None
    return 403, {
        "error": "disabled",
        "message": f"Scritture disattivate: imposta {WRITE_ENABLED_ENV}=1.",
    }


def _bankroll():
    return float(os.getenv("BANKROLL_DEFAULT", "100.0"))


def _odds_json(params=None):
    """Legge le quote dalla tabella 'signals' o match_analysis come asset curato."""
    conn = _get_conn()
    c = conn.cursor()
    rows = c.execute(
        "SELECT evento, esito, quota, probabilita, ev FROM signals "
        "WHERE esito_finale IS NULL ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return [
        {"evento": e, "esito": s, "quota": q, "probabilita": p, "ev": v}
        for e, s, q, p, v in rows if q
    ]


def _storico_json(params=None):
    signals = get_signals(limit=30)
    out = []
    for s in signals:
        if not s.quota:
            continue
        outcome = s.esito_finale or "pending"
        profit = s.profit if s.profit else None
        out.append({
            "id": s.id,
            "evento": s.evento,
            "esito": s.esito,
            "quota": s.quota,
            "probabilita": s.probabilita,
            "ev": s.ev,
            "risultato": outcome,
            "profit": profit,
            "data": s.timestamp,
        })
    summary = get_performance_summary(days=30)
    return {"segnali": out, "summary": summary}


def _dashboard_json(params=None):
    summary = get_performance_summary(days=30)
    value = _odds_json()
    # count segnali odierni
    conn = sqlite3.connect(str(DB))
    today_count = 0
    try:
        today_count = conn.cursor().execute(
            "SELECT COUNT(*) FROM signals WHERE date(timestamp) = date('now')"
        ).fetchone()[0]
    except Exception:
        today_count = 0
    conn.close()
    roi = summary.get("roi", 0.0)
    closed = summary.get("closed", 0)
    # P1 (19/10/2026): qui NON si salda piu' nulla. Una GET che scrive il
    # ledger non e' una lettura: la dashboard poteva chiudere righe (e
    # consumare crediti nel percorso di settlement) se richiesta da un
    # crawler o da un'anteprima. Il settlement resta nei job del bot.
    per_mercato = {}
    try:
        from tracker import predictions_summary
        per_mercato = predictions_summary()
    except Exception:
        pass
    # Dati reali dal DB: bankroll (colonna importo), streak, CLV, puntate
    # automatiche, drawdown — stessi numeri del report Telegram/backtest.
    extra = {}
    try:
        from performance_report import get_performance
        perf = get_performance(days=30)
        extra = {
            "streaks": perf.get("streaks", {}),
            "clv": perf.get("clv", {}),
            "auto_bets": perf.get("bets", {}),
            "bankroll_stats": perf.get("bankroll", {}),
            "predictions": perf.get("predictions", {}),
        }
    except Exception:
        pass
    # Bankroll REALE, in ordine di autorevolezza (fix 27/10/2026): prima
    # l'EQUITY del wallet SX (la stessa base su cui il bot dimensiona), poi la
    # cassa simulata, poi `BANKROLL_DEFAULT`. Prima si leggeva solo la cassa e
    # la dashboard mostrava **100.00** mentre il wallet conteneva 16,24 USDC:
    # un numero che non era ne' il patrimonio ne' un errore — la classe di
    # testo che mente del 13/09. `bankroll_source` dichiara da dove viene.
    bankroll = _bankroll()
    bankroll_source = "default"
    try:
        from order_watch import current_equity
        _eq = current_equity()
        if _eq and float(_eq) > 0:
            bankroll = round(float(_eq), 4)
            bankroll_source = "live_equity"
    except Exception:
        pass
    if bankroll_source == "default":
        try:
            from adaptive_staking import bankroll_stats
            _bs = bankroll_stats()
            if _bs.get("current"):
                bankroll = _bs["current"]
                bankroll_source = "cassa"
        except Exception:
            pass
    # Segnali di mercato (RLM/steam/crollo) sugli stessi segnali value attivi:
    # la dashboard li mostra come monitor, stessi dati del report Telegram.
    market = {}
    try:
        from market_signals import collect_market_signals, summarize_market_signals
        _signals = collect_market_signals()
        market = {
            "summary": summarize_market_signals(_signals),
            "signals": _signals,
        }
    except Exception:
        pass
    # BEAT SUL MERCATO (direttiva 04/10, punto 5): quante closing line Pinnacle
    # a T-0 sono state battute e di quanto. `rows` e' accorciato: la dashboard
    # mostra l'aggregato, il dettaglio resta nella CLI `closing_line.py`.
    closing = {}
    try:
        from closing_line import report as _closing_report
        closing = _closing_report()
        closing["rows"] = (closing.get("rows") or [])[:20]
    except Exception:
        pass
    # CLV dei SOLDI (27/10/2026): `clv` qui sopra misura le RIGHE di
    # `clv_history` (tutti i segnali, anche quelli mai giocati), quindi non
    # risponde alla domanda "le puntate vere battono il mercato?". Questo
    # blocco usa `clv_tracker` (quota d'ACQUISTO delle puntate live vs closing
    # con fonte dichiarata): la stessa misura della CLI e del comando /clv,
    # cosi' dashboard, bot e report non possono divergere.
    bets_clv = {}
    try:
        import clv_tracker
        _bundle = clv_tracker.collect(days=90)
        _summ = clv_tracker.summarize(_bundle.get("samples") or [])
        bets_clv = {
            "n": _summ.get("n"), "avg_clv": _summ.get("avg_clv"),
            "ci95": _summ.get("ci95"), "p_value": _summ.get("p_value"),
            "share_positive": _summ.get("share_positive"),
            "status": _summ.get("status"), "note": _summ.get("note"),
            "reliable": _summ.get("reliable"),
            "by_market": _summ.get("by_market"),
            "pending": len(_bundle.get("pending") or []),
            "trades": (_bundle.get("totals") or {}).get("trades"),
            "excluded": _bundle.get("excluded") or {},
        }
    except Exception as e:
        bets_clv = {"error": str(e)}
    # SALUTE DEL VOLUME (24/10/2026): il disco pieno ha fermato il bot con 231
    # errori ENOSPC senza che nessun guardrail di denaro se ne accorgesse.
    # Lo stato sta qui perche' e' una lettura a costo zero (due `stat`), e la
    # dashboard e' il posto dove un operatore lo guarda ogni giorno.
    storage = {}
    try:
        from backup_manager import storage_status
        storage = storage_status()
    except Exception as e:
        storage = {"ok": False, "level": "unknown", "error": str(e)}
    # ADEGUATEZZA DEL CAPITALE (27/10/2026): il minimo ordine dell'exchange
    # vale il 6,2% di un patrimonio di 16,24 USDC contro lo 0,56% chiesto dal
    # Kelly. E' il motivo per cui lo stake non puo' essere "gestito": la
    # dashboard lo dice insieme al capitale che servirebbe.
    capital_adequacy = {}
    try:
        import capital_adequacy as _ca
        capital_adequacy = _ca.evaluate(bankroll)
    except Exception as e:
        capital_adequacy = {"ok": False, "error": str(e)}
    return {
        "bankroll": bankroll,
        "bankroll_source": bankroll_source,
        "capital_adequacy": capital_adequacy,
        "roi_30gg": roi,
        "segnali_oggi": today_count,
        "chiusi_30gg": closed,
        "hit_rate": summary.get("hit_rate", 0.0),
        "ultime_value": value,
        "per_mercato": per_mercato,
        "market_signals": market,
        "closing": closing,
        "bets_clv": bets_clv,
        "storage": storage,
        **extra,
    }


def _schedina_json(params=None):
    """Schedina del giorno: picks con valore + multipla prolungata.

    Include stake adattivo (adaptive_staking) per ogni pick, con confidence
    weighting e drawdown protection.
    """
    try:
        from fixture_engine import get_value_picks_for_schedina, build_multipla
    except Exception as e:
        logger.warning("fixture_engine non disponibile: %s", e)
        return {"picks": [], "multipla": None, "bankroll": _bankroll()}

    # Carica adaptive staking
    try:
        from adaptive_staking import adaptive_stake, bankroll_stats
        _bs = bankroll_stats()
        # Bankroll reale solo se la cassa ha movimento, altrimenti default
        bankroll = _bs.get("current") or _bankroll()
        _peak = _bs.get("peak") or bankroll
        _adaptive = True
    except ImportError:
        _adaptive = False
        bankroll = _bankroll()
        _peak = bankroll

    picks = get_value_picks_for_schedina()
    out = []
    for p in picks:
        prob = p["ev"] + (1.0 / p["quota"]) if p["quota"] else 0.0
        pick_data = {
            "league": p["league"],
            "home": p["home"],
            "away": p["away"],
            "evento": p["evento"],
            "esito": p["esito"],
            "quota": p["quota"],
            "bookmaker": p["bookmaker"],
            "ev": p["ev"],
            "prob": prob,
            "market_edge": p.get("market_edge"),
            "status": p.get("status", "value"),
        }
        # Stake adattivo per ogni pick
        if _adaptive and p["quota"] and p["quota"] > 1.0:
            as_result = adaptive_stake(
                bankroll=bankroll, prob=prob, odds=p["quota"],
                market_edge=p.get("market_edge"),
                status=p.get("status", "value"),
                peak_bankroll=_peak)
            pick_data["stake"] = as_result["stake"]
            pick_data["stake_kelly"] = as_result["kelly_fraction"]
            pick_data["stake_reason"] = as_result["reason"]
        else:
            # Fallback: 1/4 Kelly cap 3%. La formula arriva da value_filter
            # (definizione unica): una copia locale puo' divergere in silenzio.
            try:
                import value_filter as _vf
                stake = max(0.0, bankroll
                            * _vf.kelly_fraction(prob, p["quota"], 0.25))
            except Exception:
                stake = 0.0
            pick_data["stake"] = round(min(stake, bankroll * 0.03), 2)
            pick_data["stake_kelly"] = 0.25
            pick_data["stake_reason"] = "fallback (1/4 Kelly)"
        # KELLY DINAMICO del MOTORE (direttiva 04/10, punto 2): la banda
        # 0.15-0.25 scalata da EV/edge/confidenza della lega (`dynamic_
        # kelly_fraction`). E' il k che l'EXECUTION ENGINE applica davvero:
        # la schedina lo mostra accanto al frazionamento storico, cosi'
        # l'operatore vede la size che verrebbe ordinata. Fail-safe: se il
        # modulo non e' importabile la schedina resta com'era.
        try:
            from decision.stake_engine import dynamic_kelly_fraction
            _dyn = dynamic_kelly_fraction(ev=p.get("ev"),
                                          edge=p.get("market_edge"),
                                          league=p.get("league"),
                                          market=p.get("mercato")
                                          or p.get("market"))
            pick_data["kelly_dynamic"] = _dyn["kelly_fraction"]
            pick_data["kelly_strength"] = _dyn["strength"]
            pick_data["kelly_reason"] = _dyn["reason"]
        except Exception:
            pass
        out.append(pick_data)

    mp = build_multipla(picks)
    multipla = None
    if mp:
        multipla = {
            "esiti": mp["esiti"],
            "quota": mp["quota"],
            "prob": mp["prob"],
            "ev": mp["ev"],
            "legs": [
                {"esito": l["esito"], "quota": l["quota"], "evento": l["evento"]}
                for l in mp["legs"]
            ],
        }

    return {"picks": out, "multipla": multipla, "bankroll": bankroll}


def _health_json(params=None):
    quota = None
    try:
        from odds_api import get_quota
        quota = get_quota()
    except Exception:
        quota = None
    creds = {"remaining": quota[0], "cache": quota[1]} if quota else None
    # RESIDUO DI SETTLEMENT (13/09): perche' le righe restano aperte, per
    # motivo, invece di un conteggio opaco. `overdue_orphans` DEVE essere 0:
    # se sale, la scadenza automatica delle righe insaldabili non sta girando.
    # `estimated_credits` = costo atteso del prossimo giro di refertazione.
    settlement = None
    try:
        from tracker import settlement_residue
        settlement = settlement_residue()
    except Exception:
        settlement = None
    # Refertazione e quote/CLV: the-odds-api (API-Football solo storico
    # ratings 2022-2024). Dal 06/09 l'ESECUZIONE passa dall'aggregatore
    # (execution_engine.py, BetInAsia BLACK/MollyBet). Il flag
    # betfair_enabled resta per compatibilita col frontend, sempre False.
    # VOLUME/BACKUP in forma COMPATTA: l'health e' il posto dove un monitor
    # esterno guarda (Railway, uptime checker) e il disco pieno del 24/10 era
    # invisibile proprio li'. Il dettaglio completo sta in /api/dashboard.
    storage = None
    try:
        from backup_manager import storage_status
        _st = storage_status()
        storage = {"level": _st.get("level"),
                   "free_mb": (_st.get("volume") or {}).get("free_mb"),
                   "free_pct": (_st.get("volume") or {}).get("free_pct"),
                   "backups": (_st.get("backups") or {}).get("count"),
                   "latest_backup": (_st.get("backups") or {}).get("latest_status"),
                   "warnings": _st.get("warnings") or []}
    except Exception as e:
        storage = {"level": "unknown", "error": str(e), "warnings": []}
    return {"status": "ok", "api_football_key": bool(os.getenv("API_FOOTBALL_KEY")),
            "quota": creds, "settlement": settlement, "storage": storage,
            "betfair_enabled": False, "betfair": None}


def _credits_json(params=None):
    """Crediti the-odds-api: lettura AUTOREVOLE + consumo misurato.

    GET /api/credits

    `remaining` e' l'ULTIMA lettura del contatore (`x-requests-remaining`, la
    stessa fonte di `odds_api.get_remaining`): e' il valore su cui si basano
    `status` e `sustainable_daily`. Prendere il MINIMO fra tutte le cache
    inchiodava il numero a quello di un file vecchio e faceva sembrare il
    piano quasi esaurito (12/09: reale 452, mostrato 58 -> la guardia
    proattiva non riduceva le leghe e nessun alert scattava).
    `remaining_min` resta come DIAGNOSTICA: e' il valore prudente piu' basso
    fra le cache e serve solo a vedere quanto e' stantia la telemetria.

    `estimated_daily_consumption` e' il consumo MISURATO (finestra 48h, la
    stessa del watchdog Telegram) e `days_left_at_current_rate` la proiezione:
    sono i numeri da guardare per capire se il budget regge fino al reset (una
    stima scritta a mano invecchia in silenzio).
    """
    from odds_api import (SPORTS_MAP, credit_burn_rate, credits_reset,
                          days_to_reset)
    import json, os, glob
    from pathlib import Path
    from datetime import datetime, timedelta

    remaining = []
    sport_details = []
    readings = []        # (ts_lettura, crediti): base del consumo misurato
    title_by_key = {v: k for k, v in SPORTS_MAP.items()}
    if DATA_DIR.exists():
        for f in sorted(DATA_DIR.glob("toa_*.json")):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is None:
                    continue
                rem = int(d["remaining"])
                # Nome cache -> sport key: `toa_<key>.json` (quote) oppure
                # `toa_scores_<key>.json` (punteggi). La vecchia estrazione
                # lasciava il ".json" attaccato e cercava le chiavi sbagliate.
                key = f.name[len("toa_"):-len(".json")]
                if key.startswith("scores_"):
                    key = key[len("scores_"):]
                sport_title = title_by_key.get(key, key)
                # `remaining_ts` e' l'istante della LETTURA del credito: in
                # `fetch_scores` il `ts` puo' essere quello della cache
                # precedente (payload riusato), quindi non e' affidabile.
                ts = d.get("remaining_ts", d.get("ts"))
                updated = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M UTC") \
                    if isinstance(ts, (int, float)) else str(ts)
                remaining.append(rem)
                if isinstance(ts, (int, float)):
                    readings.append((float(ts), rem))
                sport_details.append({
                    "sport": sport_title,
                    "sport_key": key,
                    "remaining": rem,
                    "updated": updated,
                    "matches": len(d.get("payload", []))
                })
            except Exception:
                continue

    remaining_min = min(remaining) if remaining else None
    # Lettura AUTOREVOLE: la piu' recente (stessa logica di _latest_credits).
    latest = max(readings, key=lambda r: r[0]) if readings else None
    rem_now = latest[1] if latest else remaining_min
    sport_count = len(remaining)

    reset_days = days_to_reset()
    # Consumo MISURATO e proiezione: stessa fonte del watchdog Telegram
    # (`odds_api.credit_burn_rate`, finestra 48h) — endpoint e alert non
    # possono raccontare due storie diverse.
    burn = credit_burn_rate(cache_dir=DATA_DIR)
    observed = burn["rate_per_day"] if burn else None
    window_h = burn["window_hours"] if burn else None
    daily_est = observed if observed is not None else 25
    sustainable_daily = rem_now / reset_days if reset_days > 0 and rem_now else 0
    days_left = (round(rem_now / observed, 1)
                 if observed and observed > 0 and rem_now else None)
    status = "ok"
    if rem_now is not None:
        if rem_now <= 5: status = "critical"
        elif rem_now <= 10: status = "danger"
        elif rem_now <= 20: status = "warning"
        elif rem_now <= 50: status = "low"

    return {
        "status": status,
        "remaining": rem_now,
        "remaining_min": remaining_min,
        "sports_cached": sport_count,
        "days_to_reset": reset_days,
        "reset_date": credits_reset().strftime("%Y-%m-%d"),
        "estimated_daily_consumption": daily_est,
        "consumption_source": "measured" if observed is not None else "heuristic",
        "observed_window_hours": window_h,
        "days_left_at_current_rate": days_left,
        "sustainable_daily": round(sustainable_daily, 1),
        "sports": sorted(sport_details, key=lambda x: x["remaining"]),
        "thresholds": {"critical": 5, "danger": 10, "warning": 20, "low": 50}
    }


def _segnali_json(params=None):
    """Segnale completo per una partita (usato dal calcolatore del sito).

    GET /api/segnali?home=Inter&away=Napoli
    Ritorna expected goals, probabilita' 1X2/Over/Under/BTTS, miglior esito
    con EV, stake Kelly (1/4, cap 3%) e, se disponibili, le quote di
    mercato reali dal calendario.
    """
    from poisson_engine import expected_goals, prob_1x2
    from value_filter import compute_ev, kelly_fraction, get_pro_stake, is_sane
    from leagues_data import ALL_LEAGUES

    home = (params.get("home") or "").strip()
    away = (params.get("away") or "").strip()
    if not home or not away:
        return 400, {"error": "Specifica home e away", "esempio": "/api/segnali?home=Inter&away=Napoli"}
    try:
        lam_h, lam_a = expected_goals(home, away)
    except Exception as e:
        return 404, {"error": str(e), "home": home, "away": away}

    p1, px, p2 = prob_1x2(lam_h, lam_a)

    # Quote di riferimento: miglior prezzo dal calendario (se gia' analizzato)
    league = None
    for l, teams in ALL_LEAGUES.items():
        if home in teams:
            league = l
            break
    market_prob = market_edge = None
    ref_odds = {}
    if league:
        try:
            from tracker import _get_conn
            conn = _get_conn(); c = conn.cursor()
            c.execute("SELECT a.best_quota, a.market_prob, a.market_edge, a.best_esito "
                      "FROM matches m JOIN match_analysis a ON a.match_id = m.id "
                      "WHERE m.home_team=? AND m.away_team=? ORDER BY a.id DESC LIMIT 1",
                      (home, away))
            row = c.fetchone(); conn.close()
            if row:
                ref_odds["quota"] = row[0]
                market_prob = row[1]
                market_edge = row[2]
        except Exception:
            pass

    candidates = [
        {"esito": "1", "label": f"Vittoria {home}", "prob": p1, "quota": 2.0},
        {"esito": "X", "label": "Pareggio", "prob": px, "quota": 3.2},
        {"esito": "2", "label": f"Vittoria {away}", "prob": p2, "quota": 2.0},
    ]
    for cand in candidates:
        cand["ev"] = compute_ev(cand["prob"], cand["quota"])
    best = max(candidates, key=lambda c: c["ev"])
    pro = get_pro_stake(100.0, best["prob"], best["quota"])
    sane, reason = is_sane(best["prob"], best["quota"], best["ev"], market_prob=market_prob)

    return {
        "home": home, "away": away, "league": league or "",
        "lam_h": round(lam_h, 3), "lam_a": round(lam_a, 3),
        "p1": round(p1, 4), "pX": round(px, 4), "p2": round(p2, 4),
        "best": {"esito": best["esito"], "label": best["label"],
                  "quota": best["quota"], "prob": round(best["prob"], 4),
                  "ev": round(best["ev"], 4)},
        "stake_kelly_pct": round(pro["stake_pct_of_bankroll"], 2),
        "sane": sane, "sane_reason": reason,
        "market": ({"prob": round(market_prob, 4), "edge": round(market_edge, 4)}
                    if market_prob is not None else None),
    }


def _calendario_json(params=None):
    """Partite del giorno con analisi (come /calendario del bot)."""
    from tracker import get_today_matches, get_analysis_for_match
    rows = get_today_matches()
    out = []
    for row in rows:
        mid, league, home, away, commence, status, _ = row
        item = {"league": league, "home": home, "away": away,
                "commence": commence, "status": status, "match_id": mid}
        ana = get_analysis_for_match(mid)
        if ana:
            (_, _, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito,
             best_quota, best_bookmaker, a_status, _, market_prob, market_edge) = ana
            item.update({
                "lam_h": lam_h, "lam_a": lam_a, "prob_1": p1, "prob_X": px,
                "prob_2": p2,
                "best_esito": best_esito, "best_quota": best_quota,
                "best_bookmaker": best_bookmaker, "best_ev": best_ev,
                "analisi_status": a_status, "market_prob": market_prob,
                "market_edge": market_edge,
            })
        out.append(item)
    return {"partite": out, "n": len(out)}


def _backtest_json(params=None):
    """Statistiche di calibrazione modello vs mercato."""
    from backtest import backtest_stats
    try:
        return backtest_stats()
    except Exception as e:
        return 500, {"error": str(e)}


def _campionati_json(params=None):
    """Lista campionati e squadre per la ricerca del calcolatore."""
    from leagues_data import ALL_LEAGUES
    return {"campionati": [{"nome": name, "squadre": sorted(teams.keys())}
                            for name, teams in ALL_LEAGUES.items()]}


def _backup_cassa_rows(rows: list) -> Path | None:
    """Salva le righe della cassa PRIMA della cancellazione (fail-closed).

    Ritorna il path del backup, oppure None se il file non e' stato scritto:
    in quel caso il chiamante NON cancella nulla. Scrittura atomica
    (tmp + fsync + os.replace) come gli altri stati persistenti su volume.
    """
    try:
        bdir = DATA_DIR / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = bdir / ("cassa_" + now.strftime("%Y%m%dT%H%M%S%f") + ".json")
        tmp = path.with_suffix(".json.tmp")
        payload = {"ts": now.isoformat(), "righe": rows}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path
    except Exception as exc:
        logger.error("api: backup cassa fallito (%s): nessuna cancellazione", exc)
        return None


def _cassa_get(params=None):
    """GET /api/cassa — LETTURA PURA (P1, 19/10/2026).

    Prima eseguiva il settlement della cassa dentro una GET: una lettura che
    scrive puo' essere innescata da qualunque pagina o anteprima, non e'
    idempotente e non ha autenticazione. Il saldo resta aggiornato dai job di
    settlement del bot (watchdog 4h + job serali); qui non si tocca il ledger.
    """
    from tracker import get_cassa, cassa_totals
    entries = get_cassa()
    return {"scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_post(params=None, body=None):
    """POST /api/cassa — ADMIN: inserisce una scommessa e salda la cassa."""
    blocked = _write_disabled()
    if blocked:
        return blocked
    from tracker import save_cassa_entry, get_cassa, cassa_totals, settle_cassa
    data = body or {}
    partita = str(data.get("partita") or "").strip()
    esito = str(data.get("esito") or "").strip()
    try:
        quota = float(data.get("quota") or 0)
        importo = float(data.get("importo") or 0)
        ev = float(data.get("ev") or 0)
    except (TypeError, ValueError):
        return 400, {"error": "quota/importo/ev non numerici"}
    if not partita or not esito or quota <= 1.0 or importo <= 0:
        return 400, {"error": "Campi mancanti o non validi: servono partita, esito, quota>1, importo>0"}
    save_cassa_entry(partita, esito, quota, importo, ev=ev, data=data.get("data"))
    try:
        settle_cassa()
    except Exception:
        pass
    entries = get_cassa()
    return {"ok": True, "scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_delete(params=None, body=None):
    """DELETE /api/cassa — ADMIN + conferma esplicita + backup (P1).

    Tre protezioni contro la cancellazione accidentale:
    1. token admin (chi puo');
    2. `API_WRITE_ENABLED=1` (se puo');
    3. `confirm=CANCELLA_CASSA` (cosa sta facendo) e, prima del DELETE, un
       backup JSON su volume. Se il backup fallisce NON si cancella nulla.

    `dry_run=1` ritorna il conteggio senza toccare il ledger.
    """
    blocked = _write_disabled()
    if blocked:
        return blocked
    from tracker import clear_cassa, get_cassa, cassa_totals
    data = body or {}
    params = params or {}
    rows = get_cassa()
    dry = str(data.get("dry_run") or params.get("dry_run") or "").lower() in (
        "1", "true", "yes", "on")
    if dry:
        return {"ok": True, "dry_run": True, "righe": len(rows),
                "eliminate": 0, "totali": cassa_totals(rows)}
    confirm = str(data.get("confirm") or params.get("confirm") or "").strip()
    if confirm != CASSA_DELETE_CONFIRM:
        return 428, {
            "error": "confirmation_required",
            "message": "Per svuotare la cassa invia confirm="
                       + CASSA_DELETE_CONFIRM
                       + " (oppure dry_run=1 per il conteggio).",
            "righe": len(rows),
        }
    # Conta PRIMA di cancellare: `rows` e' lo snapshot letto sopra, e un
    # clear che mutasse la lista (o un DB che si svuota) non deve far
    # riportare "0 righe eliminate" su una cancellazione avvenuta.
    count = len(rows)
    backup = _backup_cassa_rows(rows)
    if backup is None:
        return 500, {"error": "backup_failed",
                     "message": "Backup non scritto: cancellazione annullata."}
    clear_cassa()
    logger.warning("api: cassa svuotata (%d righe, backup %s)", count, backup)
    return {"ok": True, "eliminate": count, "backup": str(backup),
            "scommesse": [], "totali": cassa_totals([])}


def _analisi_json(params=None, body=None):
    """Rigenera calendario + analisi con la strategia corrente (come /analisi del bot).

    POST /api/analisi — esegue fetch_and_analyze_today e ritorna il riepilogo.

    P1 (19/10/2026): rotta ADMIN (protetta in `_dispatch`) + interruttore
    `API_WRITE_ENABLED=1`. La scansione consuma crediti the-odds-api e
    riscrive il ledger: non puo' essere innescata da un anonimo.
    """
    blocked = _write_disabled()
    if blocked:
        return blocked
    try:
        from fixture_engine import fetch_and_analyze_today
        from rating_engine import compute_ratings
        # Ricalcola prima i rating dai risultati piu' recenti, cosi' l'analisi
        # usa sempre la forma corrente del modello (le stime dinamiche entrano
        # in gioco con campioni sufficienti, MIN_MATCHES=6).
        try:
            compute_ratings()
        except Exception as e:
            logger.warning("ricalcolo rating fallito: %s", e)
        total, value, skipped = fetch_and_analyze_today()
        return {"ok": True, "partite": total, "value": value,
                "saltate": skipped}
    except Exception as e:
        logger.exception("errore analisi")
        return 500, {"error": str(e)}


def _db_stats_json(params=None):
    """Diagnostica: conteggi del DB (ratings, risultati, analisi)."""
    conn = sqlite3.connect(str(DB))
    c = conn.cursor()
    out = {}
    for table, q in [
        ("match_results", "SELECT COUNT(*) FROM match_results"),
        ("team_ratings", "SELECT COUNT(*) FROM team_ratings"),
        ("match_analysis", "SELECT COUNT(*) FROM match_analysis"),
        ("signals", "SELECT COUNT(*) FROM signals"),
        ("cassa", "SELECT COUNT(*) FROM cassa"),
    ]:
        try:
            out[table] = c.execute(q).fetchone()[0]
        except Exception:
            out[table] = None
    try:
        out["ultima_analisi"] = c.execute(
            "SELECT timestamp FROM match_analysis ORDER BY id DESC LIMIT 1").fetchone()[0]
    except Exception:
        out["ultima_analisi"] = None
    try:
        out["ratings_sotto_soglia"] = c.execute(
            "SELECT COUNT(*) FROM team_ratings WHERE (n_home + n_away) < 6").fetchone()[0]
    except Exception:
        out["ratings_sotto_soglia"] = None
    conn.close()
    return out


def _ratings_json(params=None):
    """Coefficienti di rating per squadre specifiche (?teams=Roma,Barcelona)."""
    teams = [t.strip() for t in (params.get("teams") or "").split(",") if t.strip()]
    conn = sqlite3.connect(str(DB))
    c = conn.cursor()
    out = []
    for t in teams:
        try:
            row = c.execute("SELECT team, league, attack_home, defense_home, attack_away, defense_away, n_home, n_away "
                            "FROM team_ratings WHERE team=?", (t,)).fetchone()
        except Exception:
            row = None
        out.append({"team": t, "rating": row})
    conn.close()
    return {"ratings": out}


def _admin_chat_ids_from_env() -> list:
    """Chat ID del proprietario (ADMIN_CHAT_ID, virgola-separati)."""
    ids = []
    for part in os.getenv("ADMIN_CHAT_ID", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def _telegram_send_message(token: str, chat_id: int, text: str):
    """Invio diretto a Telegram via API (stdlib, nessuna dipendenza)."""
    import urllib.request
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auto_bet(params=None, body=None):
    """POST /api/auto_bet — ADMIN ONLY (R1, 19/10/2026).

    Esegue SUBITO un giro di puntate, come il job delle 08:50. La rotta e'
    protetta a monte da `admin_check` (token) e da `API_AUTO_BET_ENABLED`.

    Modalita': di default il giro e' FORZATO in SIM (`mode_override="sim"`),
    qualunque sia `AUTO_BET_MODE`/kill switch, quindi l'HTTP non puo'
    piazzare ordini reali senza `API_AUTO_BET_ALLOW_LIVE=1`. E' una difesa
    INDIPENDENTE dal token: un token rubato non basta a ordinare.
    """
    if not _env_flag(AUTO_BET_ENABLED_ENV, default=False):
        return 403, {
            "error": "disabled",
            "message": f"Rotta disattivata: imposta {AUTO_BET_ENABLED_ENV}=1 "
                       "per abilitare /api/auto_bet.",
        }
    allow_live = _env_flag(AUTO_BET_ALLOW_LIVE_ENV, default=False)
    mode_override = None if allow_live else "sim"
    if mode_override == "sim":
        logger.warning("api: /api/auto_bet forzato in SIM "
                       "(ordini reali solo con %s=1)", AUTO_BET_ALLOW_LIVE_ENV)
    try:
        from auto_bet import run_today_bets
        placed = run_today_bets(allow_sim=True, mode_override=mode_override)
    except Exception as e:
        logger.exception("auto_bet endpoint")
        return 500, {"error": str(e)}
    if not placed:
        return {"ok": True, "piazzate": 0, "puntate": [],
                "sim_forzata": mode_override == "sim",
                "nota": "Nessuna puntata: nessun segnale o guardie attive (vedi log)."}
    mode = placed[0]["mode"]
    mode_label = {"live": "LIVE", "sim": "SIMULAZIONE", "dry-run": "DRY-RUN"}.get(mode, mode)
    total = sum(p["stake"] for p in placed)
    rows = "\n".join(
        f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
        f"(€{p['stake']:.2f})" for p in placed)
    text = (f"🎯 *PUNTATE AUTOMATICHE ({mode_label})*\n"
            f"{len(placed)} puntate, €{total:.2f} di stake\n\n{rows}\n\n"
            f"📌 {'ORDINI REALI' if mode == 'live' else 'Simulazione: nessun ordine reale inviato.'}")
    token = os.getenv("QUOTAVERACE_BOT_TOKEN", "")
    results = []
    for chat_id in _admin_chat_ids_from_env():
        if not token:
            break
        try:
            _telegram_send_message(token, chat_id, text)
            results.append({"chat_id": chat_id, "ok": True})
        except Exception as e:
            results.append({"chat_id": chat_id, "ok": False, "error": str(e)})
    return {"ok": True, "piazzate": len(placed), "puntate": placed,
            "sim_forzata": mode_override == "sim", "notifiche": results}


def _test_notify(params=None, body=None):
    """POST /api/test_notify — invia una notifica di prova su Telegram
    ai chat di ADMIN_CHAT_ID (+ opzionale ?chat_id= per test mirati).

    R1 (19/10/2026): NON e' piu' registrata fra le rotte GET — una GET con
    effetto collaterale (invio Telegram) non e' una rotta di monitoraggio.
    La chiave resta accettata da body/query per retrocompatibilita' dei
    chiamanti esistenti: il metodo corretto e' POST con `key` nel body.

    Sicurezza: attivo SOLO se TEST_NOTIFY_KEY e' configurato; senza la
    chiave giusta (parametro key) risponde 401. Disattivato = 403.
    """
    params = params or {}
    body = body or {}
    key = os.getenv("TEST_NOTIFY_KEY", "")
    if not key:
        return 403, {"error": "disabled",
                     "message": "Endpoint disattivato: configura TEST_NOTIFY_KEY "
                                 "nelle variabili di Railway."}
    supplied = str(params.get("key") or body.get("key") or "")
    # Confronto a tempo costante (stessa regola del token admin): un confronto
    # `!=` e' misurabile byte per byte e su una chiave di servizio non si usa.
    if not hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8")):
        return 401, {"error": "unauthorized",
                     "message": "Chiave TEST_NOTIFY_KEY errata."}

    chat_ids = _admin_chat_ids_from_env()
    extra = params.get("chat_id") or body.get("chat_id")
    if extra and str(extra).lstrip("-").isdigit():
        chat_ids.append(int(extra))
    if not chat_ids:
        return 400, {"error": "no_recipients",
                     "message": "Nessun destinatario: imposta ADMIN_CHAT_ID "
                                 "nelle variabili di Railway."}

    token = os.getenv("QUOTAVERACE_BOT_TOKEN", "")
    if not token:
        return 503, {"error": "no_bot_token",
                     "message": "QUOTAVERACE_BOT_TOKEN mancante in questo "
                                 "ambiente web_api."}

    text = params.get("text") or body.get("text") \
        or "🔔 *Test notifica* — QuotaVerace è online e funzionante ✅"
    results = []
    all_ok = True
    for cid in chat_ids:
        try:
            resp = _telegram_send_message(token, cid, text)
            results.append({"chat_id": cid, "ok": bool(resp.get("ok"))})
            if not resp.get("ok"):
                all_ok = False
        except Exception as e:  # noqa: BLE001
            results.append({"chat_id": cid, "ok": False, "error": str(e)})
            all_ok = False
    return (200 if all_ok else 502), {
        "ok": all_ok, "inviata_a": len(results), "destinatari": chat_ids,
        "results": results,
    }


def _training_json(params=None):
    """GET /api/training — dataset di addestramento ML (righe chiuse)."""
    params = params or {}
    try:
        from ml_dataset import build_training_rows
        limit = params.get("limit") or "500"
        rows = build_training_rows(limit=int(limit))
    except Exception as e:
        logger.exception("errore /api/training")
        return {"error": str(e), "n": 0, "rows": []}
    return {"n": len(rows), "rows": rows}


def _market_signals_json(params=None):
    """GET /api/market_signals — RLM/steam/crollo quota sui segnali attivi.

    Stessi rilevatori dell'alert Telegram (rlm_alert): steam >6% in <30min,
    crollo >=5% dal primo snapshot, RLM (movimento contro il pubblico).
    """
    try:
        from market_signals import collect_market_signals, summarize_market_signals
        signals = collect_market_signals()
    except Exception as e:
        logger.exception("errore /api/market_signals")
        return {"summary": {"total": 0, "urgent": 0,
                            "by_type": {"steam": 0, "crash": 0, "rlm": 0}},
                "signals": [], "error": str(e)}
    return {"summary": summarize_market_signals(signals), "signals": signals}


def _significance_json(params=None):
    """GET /api/significance — significativita' statistica del campione.

    Legge il ledger (sola lettura, zero crediti) e dice, per la popolazione
    GIOCABILE e per ogni mercato, se il ROI osservato e' distinguibile da zero
    e l'edge minimo rilevabile col campione attuale. E' il numero che rende
    leggibili le soglie di decisione del progetto ("30-40 chiusure dell'era
    nuova", "20 chiusure OU"): un ROI su 8 righe e' rumore con un segno.

    Query opzionali: `?since=YYYY-MM-DD&odds_min=1.30&odds_max=1.80` (era e
    fascia quota, gli stessi filtri dei report) e `?all=1` per la modalita'
    CONFRONTO su tutto il ledger (non decisionale).
    """
    p = params or {}
    try:
        import significance

        def _num(key):
            try:
                return float(p[key]) if p.get(key) not in (None, "") else None
            except (TypeError, ValueError):
                return None

        all_statuses = str(p.get("all", "")).strip().lower() in ("1", "true", "yes")
        data = significance.from_ledger(
            since=p.get("since") or None,
            odds_min=_num("odds_min"), odds_max=_num("odds_max"),
            by_market=True, all_statuses=all_statuses)
        data["report"] = significance.format_report(data)
        return data
    except Exception as e:
        logger.exception("errore /api/significance")
        return {"status": "error", "error": str(e)}


def _agents_json(params=None):
    """GET /api/agents — TABELLA DI COMANDO degli agenti (sola lettura).

    Una card per agente (Dati, Strategia, Analisi, Cervello, Finanza, Sizing,
    Advisor, Esecuzione) con lo STATO dell'ultimo ciclo, gli indicatori del
    contratto e l'attivita' nella finestra; piu' sicurezza (kill switch, stop
    giornaliero/settimanale, pausa settlement, gate di mercato), capitale
    (equity riconciliata, recinto, tetto per ordine) e flusso (decisioni,
    residuo di refertazione).

    Tutte le soglie e gli stati arrivano dalle LORO fonti (`agent_board` delega
    a `auto_bet`/`tracker`): nessun numero ricopiato in questo file, cosi' la
    tabella non puo' mostrare una regola diversa da quella applicata.

    Query: `?days=N` (finestra degli aggregati, default `AGENT_BOARD_DAYS`).
    Fail-safe: un errore e' un campo `error`, mai un 500 sul browser.
    """
    p = params or {}
    try:
        import agent_board
        days = None
        if str(p.get("days") or "").strip():
            try:
                days = float(p["days"])
            except (TypeError, ValueError):
                days = None
        return agent_board.board(days)
    except Exception as e:
        logger.exception("errore /api/agents")
        return {"error": str(e), "agents": [], "generated_at": None}


def _drift_json(params=None):
    """GET /api/drift — stato del drift modello (verifica REMOTA).

    Stesso check del drift watchdog di bot.py: Brier/LogLoss rolling vs
    baseline sulle previsioni chiuse. Utile per il monitoraggio continuo
    esterno (cron/uptime) senza dover entrare nel container.
    """
    try:
        from drift_monitor import check_drift
        return check_drift()
    except Exception as e:
        logger.exception("errore /api/drift")
        return {"status": "error", "error": str(e)}


def _calibration_json(params=None):
    """GET /api/calibration — dashboard calibrazione del modello.

    Istantanea completa per la pagina webapp /calibrazione:
      - stato drift (stesso check di /api/drift: Brier/LogLoss rolling
        vs baseline sulle previsioni chiuse);
      - metriche di training dell'ensemble (modello, acc, Brier, peso);
      - stato calibrazione isotonica (pre/post Brier/ECE, curva
        score->prob calibrata dal calibratore fit sul volume);
      - reliability diagram calcolato sulle previsioni chiuse (confidenza
        vs frequenza empirica, 10 bin) per la curva di calibrazione;
      - drift_history: serie temporale walk-forward del Brier rolling
        (drift_monitor.brier_history) per il grafico di tendenza.
    """
    import datetime as _dt
    import numpy as np
    from ml_ensemble import get_ensemble, MODEL_PATH
    from drift_monitor import (check_drift, load_settled_predictions,
                               brier_history)
    from probability_calibration import ECE_BINS

    try:
        ens = get_ensemble()
        tm = ens.train_metrics or {}

        model = {
            "trained": bool(ens.trained),
            "model_type": ens.model_type if ens.trained else None,
            "ensemble_weight": (round(ens.ensemble_weight, 3)
                                if ens.trained else None),
            "train_metrics": {
                "accuracy": tm.get("accuracy"),
                "brier_score": tm.get("brier_score"),
                "n_samples": tm.get("n_samples"),
                "model": tm.get("model_type"),
            },
        }

        cal_info = tm.get("calibration") or {}
        # calibration_report() (fit riuscito) NON mette "status": il
        # dict ha solo pre/post brier/ece — lo deriviamo dal calibratore.
        cal_fitted = bool(ens.calibrator is not None
                          and ens.calibrator.fitted_)
        cal = {
            "status": ("fitted" if cal_fitted
                       else cal_info.get("status", "skipped")),
            "min_required": cal_info.get("min_required"),
            "n_cal": cal_info.get("n"),
            "fitted": cal_fitted,
            "pre_brier": cal_info.get("pre_brier"),
            "post_brier": cal_info.get("post_brier"),
            "pre_ece": cal_info.get("pre_ece"),
            "post_ece": cal_info.get("post_ece"),
            "brier_improvement": cal_info.get("brier_improvement"),
            "curve": [],
        }
        if ens.calibrator is not None and ens.calibrator.fitted_:
            xs = np.asarray(ens.calibrator.x_)
            ys = np.asarray(ens.calibrator.y_)
            cal["curve"] = [
                {"score": round(float(x), 4),
                 "calibrated": round(float(y), 4)}
                for x, y in zip(xs, ys)
            ]

        # Reliability diagram dalle previsioni chiuse (confidenza vs
        # frequenza empirica, bin a larghezza uguale come l'ECE).
        preds = load_settled_predictions()
        reliability = []
        if preds:
            probs = np.array([p["prob"] for p in preds], dtype=float)
            ys_arr = np.array([p["y"] for p in preds], dtype=float)
            edges = np.linspace(0.0, 1.0, ECE_BINS + 1)
            for lo, hi in zip(edges[:-1], edges[1:]):
                if hi < 1.0:
                    mask = (probs >= lo) & (probs < hi)
                else:
                    mask = (probs >= lo) & (probs <= hi)
                m = int(mask.sum())
                if m == 0:
                    continue
                reliability.append({
                    "bin": f"{lo:.1f}-{hi:.1f}",
                    "n": m,
                    "confidence": round(float(probs[mask].mean()), 4),
                    "accuracy": round(float(ys_arr[mask].mean()), 4),
                })

        out = {
            "generated_at": (_dt.datetime.now(_dt.timezone.utc)
                             .isoformat().replace("+00:00", "Z")),
            "model": model,
            "calibration": cal,
            "reliability": reliability,
            "drift": check_drift(),
            "drift_history": brier_history(),
        }
        try:
            if MODEL_PATH.exists():
                st = MODEL_PATH.stat()
                out["model_file"] = {
                    "exists": True,
                    "updated_at": (_dt.datetime.fromtimestamp(
                        st.st_mtime, _dt.timezone.utc)
                        .isoformat().replace("+00:00", "Z")),
                    "size_bytes": st.st_size,
                }
        except Exception:
            pass
        return out
    except Exception as e:
        logger.exception("errore /api/calibration")
        return {"error": str(e)}


def _scan_json(params=None):
    """Endpoint rimosso (04/09): lo scanner di catalogo Exchange non esiste
    più. Dal 06/09 l'esecuzione passa dall'aggregatore via
    execution_engine.py (BetInAsia BLACK / MollyBet, protocollo
    Betfair-compatible); la refertazione resta the-odds-api.
    """
    return 503, {
        "error": "betfair_removed",
        "message": "Scanner Exchange rimosso. Esecuzione via aggregatore "
                   "(execution_engine.py); refertazione: the-odds-api.",
    }


ROUTES = {
    "/api/health": _health_json,
    "/api/credits": _credits_json,
    "/api/dashboard": _dashboard_json,
    "/api/storico": _storico_json,
    "/api/value": _odds_json,
    "/api/schedina": _schedina_json,
    "/api/scan": _scan_json,
    "/api/segnali": _segnali_json,
    "/api/calendario": _calendario_json,
    "/api/backtest": _backtest_json,
    "/api/campionati": _campionati_json,
    "/api/cassa": _cassa_get,
    "/api/db_stats": _db_stats_json,
    "/api/ratings": _ratings_json,
    "/api/training": _training_json,
    "/api/market_signals": _market_signals_json,
    "/api/drift": _drift_json,
    "/api/calibration": _calibration_json,
    "/api/significance": _significance_json,
    "/api/agents": _agents_json,
}

POST_ROUTES = {
    "/api/cassa": _cassa_post,
    "/api/analisi": _analisi_json,
    "/api/test_notify": _test_notify,
    "/api/auto_bet": _auto_bet,
}

DELETE_ROUTES = {
    "/api/cassa": _cassa_delete,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch(ROUTES, None)

    def do_POST(self):
        self._dispatch(POST_ROUTES, self._read_body())

    def do_DELETE(self):
        # P1: il corpo della DELETE serve per `confirm`/`dry_run`. Senza
        # leggerlo, una cancellazione protetta non potrebbe mai essere
        # confermata via JSON (resterebbe solo la query string).
        self._dispatch(DELETE_ROUTES, self._read_body())

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _dispatch(self, routes, body):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        handler = routes.get(path)
        if not handler:
            self._send(404, {"error": "not found", "routes": list(ROUTES.keys())})
            return
        # R1/P1: le rotte admin si autorizzano PRIMA di eseguire qualunque
        # cosa. Il permesso e' per METODO: `GET /api/cassa` e' una lettura
        # pubblica, `POST`/`DELETE` sullo stesso path sono admin.
        method = (self.command or "").upper()
        if path in ADMIN_METHOD_ROUTES.get(method, frozenset()):
            ok, code, payload = admin_check(self.headers)
            if not ok:
                logger.warning("api: rotta admin negata %s %s (%s)",
                               method, path, payload.get("error"))
                self._send(code, payload)
                return
        # P1: permesso di LETTURA separato (health sempre pubblica).
        if method == "GET" and path not in READ_PUBLIC_ROUTES:
            ok, code, payload = read_check(self.headers)
            if not ok:
                logger.warning("api: lettura negata %s (%s)",
                               path, payload.get("error"))
                self._send(code, payload)
                return
        try:
            result = handler(params, body) if body is not None else handler(params)
        except Exception as e:
            logger.exception("errore endpoint %s", path)
            self._send(500, {"error": str(e)})
            return
        if isinstance(result, tuple) and len(result) == 2:
            self._send(*result)
        else:
            self._send(200, result)

    def _send(self, code, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Alcune rotte accettano ancora `key` in query string (retrocompat):
        # senza redazione la chiave finirebbe nei log del container.
        msg = _SECRET_QUERY_RE.sub(r"\1***", fmt % args)
        logger.info("%s - %s", self.address_string(), msg)


def main():
    import secure_logging
    secure_logging.setup()  # maschera segreti nei log + httpx a WARNING
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _get_conn().close()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("QuotaVerace Web API in ascolto su :%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()