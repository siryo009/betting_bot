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

Uso su Railway: dopo aver deployato il bot, crea un secondo servizio con
  startCommand = "python web_api.py"
oppure avvialo a fianco del bot. Porta di default 8000 (override via WEB_API_PORT).
"""

import json
import logging
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import DATA_DIR
from tracker import _get_conn, _create_results_table, get_performance_summary, get_signals

logger = logging.getLogger("web_api")

# Railway injects PORT; fall back to WEB_API_PORT, then 8000 for local use.
PORT = int(os.getenv("PORT") or os.getenv("WEB_API_PORT") or "8000")
DB = DATA_DIR / "quotaverace.db"


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
    # Salda previsioni e cassa in modo idempotente prima di mostrare i numeri.
    try:
        from tracker import settle_predictions, settle_cassa
        settle_predictions()
        settle_cassa()
    except Exception:
        pass
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
    # Bankroll REALE (SUM cassa.importo) invece del default env: si usa il
    # valore reale SOLO se c'e' del movimento in cassa, altrimenti resta il
    # bankroll di riferimento (BANKROLL_DEFAULT).
    bankroll = _bankroll()
    try:
        from adaptive_staking import bankroll_stats
        _bs = bankroll_stats()
        if _bs.get("current"):
            bankroll = _bs["current"]
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
    return {
        "bankroll": bankroll,
        "roi_30gg": roi,
        "segnali_oggi": today_count,
        "chiusi_30gg": closed,
        "hit_rate": summary.get("hit_rate", 0.0),
        "ultime_value": value,
        "per_mercato": per_mercato,
        "market_signals": market,
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
            # Fallback: 1/4 Kelly cap 3%
            kelly_full = (prob * p["quota"] - 1) / (p["quota"] - 1) if p["quota"] > 1 else 0
            stake = max(0, kelly_full * 0.25 * bankroll)
            pick_data["stake"] = round(min(stake, bankroll * 0.03), 2)
            pick_data["stake_kelly"] = 0.25
            pick_data["stake_reason"] = "fallback (1/4 Kelly)"
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
    return {"status": "ok", "api_football_key": bool(os.getenv("API_FOOTBALL_KEY")),
            "quota": creds, "settlement": settlement,
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
    from odds_api import (CREDITS_RESET, SPORTS_MAP, credit_burn_rate,
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
        "reset_date": CREDITS_RESET.strftime("%Y-%m-%d"),
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


def _cassa_get(params=None):
    """Cassa con saldo automatico: le scommesse chiuse sono aggiornate
    coi risultati reali (idempotente, le pending restano pending)."""
    from tracker import get_cassa, cassa_totals, settle_cassa
    try:
        settle_cassa()
    except Exception as e:
        logger.warning("settle_cassa al GET /api/cassa: %s", e)
    entries = get_cassa()
    return {"scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_post(params=None, body=None):
    from tracker import save_cassa_entry, get_cassa, cassa_totals, settle_cassa
    data = body or {}
    partita = (data.get("partita") or "").strip()
    esito = (data.get("esito") or "").strip()
    quota = float(data.get("quota") or 0)
    importo = float(data.get("importo") or 0)
    if not partita or not esito or quota <= 1.0 or importo <= 0:
        return 400, {"error": "Campi mancanti o non validi: servono partita, esito, quota>1, importo>0"}
    ev = float(data.get("ev") or 0)
    save_cassa_entry(partita, esito, quota, importo, ev=ev, data=data.get("data"))
    try:
        settle_cassa()
    except Exception:
        pass
    entries = get_cassa()
    return {"ok": True, "scommesse": entries, "totali": cassa_totals(entries)}


def _cassa_delete(params=None):
    from tracker import clear_cassa, cassa_totals
    clear_cassa()
    return {"ok": True, "scommesse": [], "totali": cassa_totals([])}


def _analisi_json(params=None, body=None):
    """Rigenera calendario + analisi con la strategia corrente (come /analisi del bot).

    POST /api/analisi — esegue fetch_and_analyze_today e ritorna il riepilogo.
    """
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
    """POST /api/auto_bet — esegue SUBITO le puntate del giorno (SIM-only),
    come il job delle 08:50, e notifica su Telegram gli admin.

    Dal 04/09 le puntate sono SEMPRE simulate con la quota del segnale
    (mode='sim', nessun conto Exchange): finiscono in tabella `bets` e
    vengono saldate a fine partita come sempre (ledger/ML/CLV).
    """
    try:
        from auto_bet import run_today_bets
        placed = run_today_bets(allow_sim=True)
    except Exception as e:
        logger.exception("auto_bet endpoint")
        return 500, {"error": str(e)}
    if not placed:
        return {"ok": True, "piazzate": 0, "puntate": [],
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
            "notifiche": results}


def _test_notify(params=None, body=None):
    """POST/GET /api/test_notify — invia una notifica di prova su Telegram
    ai chat di ADMIN_CHAT_ID (+ opzionale ?chat_id= per test mirati).

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
    supplied = params.get("key") or body.get("key") or ""
    if supplied != key:
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
    "/api/test_notify": _test_notify,
    "/api/training": _training_json,
    "/api/market_signals": _market_signals_json,
    "/api/drift": _drift_json,
    "/api/calibration": _calibration_json,
    "/api/significance": _significance_json,
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
        self._dispatch(DELETE_ROUTES, None)

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
        logger.info("%s - %s", self.address_string(), fmt % args)


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