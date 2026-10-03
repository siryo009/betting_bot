import json
import logging
import os
import shutil
import sqlite3
from datetime import time, datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from config import DATA_DIR
from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts
from leagues_data import ALL_LEAGUES
from tracker import (init_db, log_signal, get_signals, get_performance_summary,
                     add_subscriber, remove_subscriber, get_subscribers, set_tier,
                     get_subscription, is_premium, is_notified, mark_notified)
from odds_ingest import load_odds
from value_filter import (compute_ev, kelly_fraction, kelly_euro,
                          filter_value_bets, is_sane, get_pro_stake,
                          EV_MIN, EV_MAX, ODDS_MIN, ODDS_MAX,
                          MARKET_EDGE_MIN, MARKET_EDGE_STRONG)
from surebet_scanner import scan_surebets
from backtest import run_backtest
from football_hist import run_sync
from fixture_engine import (fetch_and_analyze_today, get_calendar_formatted,
                            get_value_picks_for_schedina, format_schedina, build_multipla_block)
from auto_bet import run_today_bets

try:
    from odds_api import get_live_odds
    LIVE_ODDS_AVAILABLE = True
except Exception:
    LIVE_ODDS_AVAILABLE = False

from config import TOKEN, BANKROLL_DEFAULT

# --- AI Commander (opsional: butuh GOOGLE_API_KEY di .env) ---
try:
    from ai_commander import AICommander
    _AI_OK = True
except Exception as _e:  # modul ada tapi dependensi/env hilang
    _AI_OK = False
    _AI_ERR = _e

import asyncio
import concurrent.futures
_ai_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_scan_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_ai_commander = None  # dibuat lazy saat pertama dipakai


def _get_ai_commander():
    global _ai_commander
    if _ai_commander is None:
        _ai_commander = AICommander()
    return _ai_commander


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai <pertanyaan bebas> — Comandante AI memilih tool mesin sendiri."""
    if not _AI_OK:
        await update.message.reply_text(
            "🤖 AI Commander tidak aktif (butuh GOOGLE_API_KEY di .env).",
            parse_mode="Markdown")
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "🤖 *AI Commander*\nTanya bebas, saya pilih tool mesinnya sendiri.\n"
            "Contoh: `/ai analisa Inter vs Napoli` atau `/ai schedina hari ini`",
            parse_mode="Markdown")
        return
    note = await update.message.reply_text("🤖 Comandante sedang menganalisa...")
    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(
            _ai_executor, lambda: _get_ai_commander().run(prompt))
    except Exception as e:
        logger.exception("AI commander gagal")
        answer = f"🤖 AI Commander gagal: {type(e).__name__}: {e}"
    # Markdown Telegram è severo: se il testo contiene caratteri non validi
    # (es. underscore in nomi squadra) mandiamo il testo grezzo.
    try:
        await note.edit_text(answer, parse_mode="Markdown")
    except Exception:
        try:
            await note.edit_text(answer)
        except Exception:
            await update.message.reply_text(answer)

import secure_logging
secure_logging.setup()  # maschera segreti nei log + httpx a WARNING (no token negli URL)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "\n\n────────────────────\n"
    "🎲 *Gioca responsabilmente*\n"
    "Le scommesse sono un gioco d'azzardo. Non puntare più di quanto puoi "
    "permetterti di perdere. Se hai bisogno di aiuto, visita il portale ADM: "
    "[www.adm.gov.it](https://www.adm.gov.it)"
)

# Riga dei filtri mostrata all'utente: derivata dalle costanti REALI di
# value_filter, cosi' non puo' piu' restare indietro rispetto alla strategia
# (prima era hardcoded: diceva "Odds 1.30-1.80 / Edge +3pp / EV 2-15%" anche
# dopo i cambi di strategia del 12-13/09).
FILTRI_TXT = (f"EV {EV_MIN*100:g}%-{EV_MAX*100:g}% | "
              f"Odds {ODDS_MIN:.2f}-{ODDS_MAX:.2f} | "
              f"Edge ≥ +{MARKET_EDGE_MIN*100:.0f}pp | Kelly frazionato | Cap 0.5-2%")

chat_bankrolls: dict[int, float] = {}

def get_bankroll(chat_id: int | None = None) -> float:
    """Bankroll del chat (BANKROLL_DEFAULT se non impostato).

    `chat_id` e' FACOLTATIVO di proposito: il messaggio di stato all'avvio non
    ha un chat. Senza default la chiamata a `bot.main()` sollevava
    `TypeError: get_bankroll() missing 1 required positional argument` e la
    PRODUZIONE andava in crash-loop (bug trovato in produzione il 19/09/2026).
    """
    if chat_id is None:
        return BANKROLL_DEFAULT
    return chat_bankrolls.get(chat_id, BANKROLL_DEFAULT)

def set_bankroll(chat_id: int, amount: float) -> None:
    chat_bankrolls[chat_id] = max(10.0, amount)

# File di fallback delle quote reali (schema odds_ingest).
ODDS_FALLBACK_FILE = DATA_DIR / "odds_sample.json"


def get_odds_data():
    if os.getenv("ODDS_API_KEY") and LIVE_ODDS_AVAILABLE:
        try:
            odds = get_live_odds()
            logger.info(f"Quote reali caricate: {len(odds)} quote")
            return odds
        except Exception as e:
            logger.warning(f"Quote reali non disponibili: {e}")
    try:
        return load_odds(str(ODDS_FALLBACK_FILE))
    except Exception as e:
        logger.warning(f"Fallback quote non disponibile: {e}")
        return []


def get_odds_freshness_note() -> str | None:
    """Nota di freschezza delle quote per i segnali manuali.

    None se le quote vengono dal feed live (ODDS_API_KEY). Altrimenti
    l'eta' del file di fallback: se vecchio, chi punta deve verificare
    il prezzo attuale sul bookmaker (le quote stale = edge finto).
    """
    if os.getenv("ODDS_API_KEY") and LIVE_ODDS_AVAILABLE:
        return None
    try:
        age_sec = datetime.now().timestamp() - ODDS_FALLBACK_FILE.stat().st_mtime
        age_min = int(age_sec / 60)
    except Exception:
        return "Quote di mercato non disponibili: segnale basato solo sul modello."
    if age_min < 60:
        return None
    return (f"Quote di mercato da cache ({age_min} min fa): verifica il prezzo "
            f"attuale sul bookmaker prima di puntare.")

def _all_teams():
    teams = set()
    for lt in ALL_LEAGUES.values():
        teams.update(lt.keys())
    return teams

# --- Sticker premium animato (gratis: nessun Telegram Premium/Fragment richiesto) ---
# Telegram non permette custom emoji nel testo senza usernames su Fragment o
# Premium sull'account proprietario. Workaround: il bot invia uno sticker
# animato (set pubblico configurabile via PREMIUM_STICKER_SET) prima dei
# messaggi premium. Mai bloccante: se fallisce si manda solo il testo.
PREMIUM_STICKER_SET = os.getenv("PREMIUM_STICKER_SET", "Diamond")
_PREMIUM_STICKER_EMOJIS = ("💎", "🔔", "⚡", "🔥", "🏆", "💰", "✅")
_premium_sticker_file_id: str | None = None


async def get_premium_sticker_file_id(bot) -> str | None:
    """File_id di uno sticker del set configurato, preferendo animato ed emoji pertinente.

    Il file_id e' stabile per bot: viene recuperato una sola volta e messo in
    cache in memoria.
    """
    global _premium_sticker_file_id
    if _premium_sticker_file_id:
        return _premium_sticker_file_id
    try:
        sticker_set = await bot.get_sticker_set(PREMIUM_STICKER_SET)
        stickers = list(sticker_set.stickers)
        if not stickers:
            return None
        def _score(s):
            return (bool(getattr(s, "is_animated", False)),
                    str(getattr(s, "emoji", "")) in _PREMIUM_STICKER_EMOJIS)
        best = max(stickers, key=_score)
        _premium_sticker_file_id = best.file_id
        logger.info("Sticker premium pronto: %s/%s (animato=%s)",
                    PREMIUM_STICKER_SET, best.emoji,
                    getattr(best, "is_animated", False))
        return _premium_sticker_file_id
    except Exception as e:
        logger.warning("Sticker set '%s' non disponibile: %s", PREMIUM_STICKER_SET, e)
        return None


async def send_premium_sticker(bot, chat_id) -> None:
    """Invia lo sticker animato prima di un messaggio premium (mai bloccante)."""
    try:
        file_id = await get_premium_sticker_file_id(bot)
        if file_id:
            await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception as e:
        logger.warning(f"Sticker premium non inviato a {chat_id}: {e}")

def format_segnale_pronto(home, away, lam_h, lam_a, bookmaker="Generico", bankroll=100.0,
                          extra_note=None):
    p1, px, p2 = prob_1x2(lam_h, lam_a)
    candidates = [
        ("1", p1, f"Vittoria {home}", 2.0), ("X", px, "Pareggio", 3.2),
        ("2", p2, f"Vittoria {away}", 2.0),
    ]
    best = max(candidates, key=lambda x: compute_ev(x[1], x[3]))
    _, best_prob, best_label, best_quota = best
    ev = compute_ev(best_prob, best_quota)
    ev_percent = ev * 100.0
    pro = get_pro_stake(bankroll, best_prob, best_quota)
    stake_euro = pro["stake"]
    sane, reason = is_sane(best_prob, best_quota, ev)
    if not sane:
        valore_label = f"🔴 *FILTRATO — {reason}*"
        raccomandazione = "❌ Segnale scartato dai filtri di sanità Pro"
    elif ev > 0.10:
        valore_label = "🟢 *FORTE VALORE*"
        raccomandazione = "✅ Raccomandato"
    elif ev > 0.03:
        valore_label = "🟡 *Valore positivo*"
        raccomandazione = "⚠️ Marginale, valutare con cautela"
    elif ev > 0:
        valore_label = "🟠 *Valore debole*"
        raccomandazione = "ℹ️ Rischio elevato — stake minimo"
    else:
        valore_label = "🔴 *Valore negativo*"
        raccomandazione = "❌ NON raccomandato"
    msg = (
        f"📊 *SEGNALE PRONTO – {home} vs {away}*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚽ *Expected Goals:*\n   {home}: {lam_h:.2f}\n   {away}: {lam_a:.2f}\n\n"
        f"📈 *Probabilità:*\n   1: {p1*100:.1f}% | X: {px*100:.1f}% | 2: {p2*100:.1f}%\n\n"
        f"🎯 *SEGNALE:* {best_label}\n   Bookmaker: {bookmaker} | Quota: {best_quota:.2f}\n"
        f"   EV: {ev_percent:+.2f}%\n\n"
        f"💰 *Kelly Pro (frazionato + cap per lega):*\n"
        f"   Bankroll: €{bankroll:.2f}\n"
        f"   Kelly grezzo: {pro['kelly_pct']:.1f}% | Cap: €{pro['stake_cap']:.2f} "
        f"({pro['stake_cap_pct']:.1f}%)\n"
        f"   *Stake finale: €{stake_euro:.2f}* ({pro['stake_pct_of_bankroll']:.1f}% bankroll)\n\n"
        f"🛡 *Filtri applicati:*\n"
        f"   {FILTRI_TXT}\n\n"
        f"{valore_label}\n{raccomandazione}\n\n📅 *Data:* oggi"
    )
    if extra_note:
        msg += f"\n\n⚠️ {extra_note}"
    return msg + DISCLAIMER

async def cmd_test_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = format_segnale_pronto("Inter", "Napoli", 1.85, 1.12,
                                 bookmaker="Bet365", bankroll=get_bankroll(chat_id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_segnale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ Errore: specifica casa e trasferta.\nEsempio: `/segnale Roma Milan`", parse_mode="Markdown")
        return
    raw = " ".join(args)
    words = raw.split()
    home, away = raw, raw
    for i in range(1, len(words)):
        h = " ".join(words[:i]); a = " ".join(words[i:])
        if h in _all_teams() and a in _all_teams():
            home, away = h, a; break
    if home not in _all_teams() or away not in _all_teams():
        await update.message.reply_text(f"❌ Squadra non trovata: {home}.\nUsa `/campionati` per la lista.", parse_mode="Markdown")
        return
    try:
        lam_h, lam_a = expected_goals(home, away)
        # OU2.5 escluso definitivamente (06/09): niente piu' lookup di quote
        # Over — il segnale usa le quote di modello 1X2 interne e il bookmaker
        # e' sempre "Modello" (con nota di caveat).
        bookmaker = "Modello"
        notes = ["Quota di MODELLO, non verificata su un bookmaker reale: "
                 "controlla il miglior prezzo disponibile prima di puntare."]
        fresh = get_odds_freshness_note()
        if fresh:
            notes.append(fresh)
        text = format_segnale_pronto(home, away, lam_h, lam_a,
                                     bookmaker=bookmaker,
                                     bankroll=get_bankroll(update.effective_chat.id),
                                     extra_note=" ".join(notes) if notes else None)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Errore segnale: {e}")
        await update.message.reply_text("❌ Errore nel calcolo. Riprova.", parse_mode="Markdown")

async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_value_bets(get_odds_data(), get_bankroll(update.effective_chat.id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_surebet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_surebets(get_odds_data())
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_storico_personale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    signals = get_signals(chat_id=chat_id, limit=20)
    if not signals:
        await update.message.reply_text("📭 Nessun segnale ricevuto.", parse_mode="Markdown"); return
    msg = "📊 *I tuoi ultimi segnali*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in signals[:10]:
        status = "✅" if s.esito_finale == "won" else "❌" if s.esito_finale == "lost" else "⏳"
        profit = f" ({s.profit:+.2f}u)" if s.profit != 0 else ""
        msg += f"{status} {s.evento}\n   {s.esito} @ {s.quota:.2f} | EV {s.ev*100:+.1f}%{profit}\n\n"
    summary = get_performance_summary(days=30)
    if summary["closed"] > 0:
        msg += f"📈 30gg: {summary['closed']} segnali | ROI: {summary['roi']:.1f}%"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setbankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text(f"💰 Bankroll: €{get_bankroll(chat_id):.2f}\nUsa `/setbankroll 500`", parse_mode="Markdown"); return
    try:
        amount = float(args[0].replace(",","."))
        set_bankroll(chat_id, amount)
        await update.message.reply_text(f"✅ Bankroll: €{amount:.2f}\n\n🛡 Sistema Pro attivo:\n• Kelly frazionato | Cap 0.5-2%\n• {FILTRI_TXT}", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Numero non valido.", parse_mode="Markdown")

async def cmd_campionati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "🏆 *Campionati disponibili*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for name, teams in ALL_LEAGUES.items(): msg += f"• *{name}*: {len(teams)} squadre\n"
    msg += "\nEsempio: `/segnale Manchester City Arsenal`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_calendario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = get_calendar_formatted()
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_analisi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Aggiornamento calendario e analisi in corso...", parse_mode="Markdown")
    total, _, _ = fetch_and_analyze_today()
    text = get_calendar_formatted()
    await update.message.reply_text(f"✅ Analizzate {total} partite.\n\n{text}", parse_mode="Markdown")

async def cmd_schedina(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    picks = get_value_picks_for_schedina()
    text = format_schedina(picks, get_bankroll(chat_id))
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_multipla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    picks = get_value_picks_for_schedina()
    block = build_multipla_block(picks, get_bankroll(chat_id))
    if not block:
        await update.message.reply_text("🎲 *MULTIPLA PROLUNGATA*\n\nServono almeno 2 esiti con valore positivo.\nRiprova dopo `/analisi`.", parse_mode="Markdown")
        return
    prefix = "🎲 *MULTIPLA PROLUNGATA*\n🗓 " + datetime.now().strftime('%d/%m/%Y') + "\n"
    await update.message.reply_text(prefix + block, parse_mode="Markdown")

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "🔔 *Iscrizione attivata!* (piano: FREE)\n\nRiceverai:\n"
        "• Schedina mattutina alle 8:00\n"
        f"• Notifiche value bet ({FILTRI_TXT})\n"
        "• Aggiornamenti pomeriggio e sera\n\n"
        "🛡 *Filtri Pro attivi:*\n"
        "• Kelly frazionato | Cap puntata 0.5-2%\n"
        f"• EV min +{EV_MIN*100:g}% | EV max +{EV_MAX*100:g}%\n"
        f"• Odds {ODDS_MIN:.2f}-{ODDS_MAX:.2f} (solo favoriti netti)\n\n"
        "💎 *Premium* (segnali istantanei, strong value, surebet): "
        "`/premium` per info.", parse_mode="Markdown")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("🔕 Disiscrizione completata.", parse_mode="Markdown")

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attiva il piano premium GRATUITAMENTE (offerta lancio).

    Nessun pagamento richiesto: chiunque puo' sbloccare premium con
    /premium. L'infrastruttura dei tier resta pronta per monetizzare
    in futuro — basta impostare PREMIUM_FREE=0 per chiudere il gate
    e richiedere il pagamento (Telegram Stars / Stripe).
    """
    chat_id = update.effective_chat.id
    add_subscriber(chat_id)
    days = 90
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass
    from datetime import timedelta
    until = (datetime.now() + timedelta(days=days)).isoformat()
    premium_free = os.getenv("PREMIUM_FREE", "1").lower() not in ("0", "false", "no")
    if premium_free:
        set_tier(chat_id, "premium", until)
        await send_premium_sticker(context.bot, chat_id)
        await update.message.reply_text(
            "💎 *Premium attivo — GRATIS!*\n\n"
            f"Scadenza: {datetime.strptime(until[:10], '%Y-%m-%d').strftime('%d/%m/%Y')} "
            "(rinnovabile sempre gratis con `/premium`)\n\n"
            "Hai sbloccato:\n"
            "• Segnali value IMMEDIATI (no ritardo di 3 ore)\n"
            "• Alert surebet in tempo reale\n"
            "• Badge 💎 sui segnali\n\n"
            "`/mytier` per lo stato. `/unsubscribe` per disiscriverti.",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "💎 *Piano Premium*\n\n"
            "Riceverai (rispetto al piano free):\n"
            "• Segnali value IMMEDIATI (il piano free li riceve in ritardo)\n"
            "• Alert surebet in tempo reale\n\n"
            "Per attivarlo contatta l'amministratore.", parse_mode="Markdown")

async def cmd_mytier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = get_subscription(update.effective_chat.id)
    if not sub:
        await update.message.reply_text(
            "Non risulti iscritto. `/subscribe` per attivare le notifiche.",
            parse_mode="Markdown")
        return
    tier, until = sub
    if tier == "premium":
        expiry = (datetime.fromisoformat(until).strftime("%d/%m/%Y")
                  if until else "senza scadenza")
        await update.message.reply_text(
            f"💎 Piano: PREMIUM (scadenza: {expiry})", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "🆓 Piano: FREE\n\n"
            "Il piano premium aggiunge: segnali immediati, strong value, "
            "surebet. `/premium` per info.", parse_mode="Markdown")

async def cmd_checknow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Controllo quote reali con filtri Pro...", parse_mode="Markdown")
    await notify_job(context)
    await update.message.reply_text("✅ Completato.", parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 *QuotaVerace Pro — Comandi*\n\n"
        "`/calendario` – partite del giorno con analisi\n"
        "`/analisi` – aggiorna calendario e analisi\n"
        "`/schedina` – schedina con filtri Pro\n"
        "`/multipla` – multipla prolungata con risk management\n"
        "`/segnale <casa> <trasferta>` – analisi specifica\n"
        "`/value` – value bet filtrate\n"
        "`/surebet` – scanner arbitraggi\n"
        "`/setbankroll <€>` – imposta bankroll\n"
        "`/autobet [off|sim|live|now]` – kill-switch/esegui ora (admin)\n"
        "`/t60reset` – disinnesca il kill switch patrimoniale T-60 (admin)\n"
        "`/stopreset` – azzera lo stop-loss giornaliero (admin)\n"
        "`/weeklyreset` – azzera il circuit breaker settimanale (admin)\n"
        "`/settlement [on|off]` – pausa settlement automatico (admin)\n"
        "`/sxscan` – scan segnali SX Bet ora (admin)\n"
        "`/subscribe` – attiva notifiche Pro\n"
        "`/risultati` – statistiche reali dei segnali\n"
        "`/backtest` – calibrazione EV atteso vs ROI realizzato\n"
        "`/sync` – sincronizza risultati storici (API-Football)\n"
        "`/quota` – crediti API rimanenti\n"
        "`/campionati` – elenco squadre\n"
        "`/ai <pertanyaan>` – Comandante AI (Gemini)\n\n"
        "🛡 *Filtri Pro attivi (solo favoriti netti):*\n"
        f"• Quota: {ODDS_MIN:.2f}–{ODDS_MAX:.2f} | EV: +{EV_MIN*100:g}% to +{EV_MAX*100:g}%\n"
        f"• Edge vs mercato: +{MARKET_EDGE_MIN*100:.0f}pp (value) / +{MARKET_EDGE_STRONG*100:.0f}pp (strong)\n"
        "• Kelly frazionato | Cap: 1% value, 2% strong\n"
        "• Stop-loss giornaliero: -5% → 24h"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def enrich_odds_with_probs(odds_data):
    from poisson_engine import expected_goals, prob_1x2, prob_over_under
    teams = _all_teams()

    def match_team(t):
        if t in teams: return t
        t_low = t.lower().replace(" fc", "").replace("cf ", "").strip()
        for tm in teams:
            tm_low = tm.lower()
            if t_low == tm_low or t_low in tm_low or tm_low in t_low:
                return tm
        return None

    enriched = []
    if hasattr(odds_data, "to_dict"):
        odds_data = odds_data.to_dict(orient="records")
    for odd in odds_data:
        evento = odd.get("evento", "")
        if " vs " not in evento: continue
        parts = evento.split(" vs ")
        home = parts[0].split(" – ")[-1].strip()
        away = parts[1].strip()
        hm, am = match_team(home), match_team(away)
        if not (hm and am and hm != am): continue
        try:
            lh, la = expected_goals(hm, am)
            p1, px, p2 = prob_1x2(lh, la)
            po, pu = prob_over_under(lh, la)
            esito = str(odd.get("esito", "")).lower()
            prob = 0.0
            if esito == "1": prob = p1
            elif esito == "x": prob = px
            elif esito == "2": prob = p2
            elif "over" in esito: prob = po
            elif "under" in esito: prob = pu
            if prob > 0:
                new_odd = odd.copy()
                new_odd["probabilita"] = prob
                new_odd["evento"] = f"{hm} vs {am}"
                enriched.append(new_odd)
        except Exception:
            pass
    return enriched

def format_value_bets(odds_data, bankroll=100.0):
    enriched = enrich_odds_with_probs(odds_data)
    value_signals = filter_value_bets(enriched, ev_threshold=EV_MIN)
    if not value_signals:
        return (f"📊 *Value Bet Pro*\n\nNessun segnale che supera i filtri "
                f"({FILTRI_TXT})." + DISCLAIMER)
    msg = "📊 *VALUE BET PRO — Filtri attivi*\n"
    msg += f"🛡 {FILTRI_TXT}\n"
    msg += "🎯 Bonus: confronto col mercato (devig power)\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for sig in value_signals[:5]:
        ev_pct = sig["ev"] * 100
        prob = sig.get("probabilita", 0)
        quota = sig.get("quota_decimale", 1.0)
        pro = get_pro_stake(bankroll, prob, quota)
        mkt_txt = ""
        if sig.get("market_edge") is not None:
            mkt_txt = f" | 🎯 mercato {sig['market_edge']*100:+.1f}pp"
        msg += (
            f"🏟 {sig['evento']}\n"
            f"🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n"
            f"📈 EV: +{ev_pct:.2f}%{mkt_txt} | Stake: €{pro['stake']:.2f} ({pro['stake_pct_of_bankroll']:.1f}%)\n\n"
        )
    msg += f"💰 Bankroll: €{bankroll:.2f}"
    return msg + DISCLAIMER

def format_surebets(odds_data):
    sures = scan_surebets(odds_data)
    if not sures: return "🔍 *Surebet*\n\nNessun arbitraggio trovato." + DISCLAIMER
    msg = "🔍 *SUREBET*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for s in sures: msg += f"🏟 {s['evento']}\n💰 Profitto: {s['profit_pct']:.2f}%\n\n"
    return msg + DISCLAIMER




def _update_results():
    """Aggiorna risultati e rating dalle API. Ritorna (updated, stats, bet_settlements).

    Ordine corretto:
      1. Scarica risultati dall'API e salva in match_results
      2. Salda cassa / previsioni / puntate auto (ora i risultati esistono)
      3. Aggiorna rating
    """
    from odds_api import (SPORTS_MAP, fetch_scores, match_scores_by_name,
                          SCORES_DAYS_FROM)
    from tracker import (save_result, get_results_stats, get_leagues_with_open_rows,
                          settle_cassa, settle_predictions, settle_bets,
                          settlement_coverage_policy)
    from rating_engine import compute_ratings
    # PAUSA SETTLEMENT (11/09/2026): durante il cambio di strategia nessuna
    # riga viene chiusa automaticamente. Si esce PRIMA di fetch_scores, cosi'
    # la pausa non consuma crediti the-odds-api.
    from tracker import settlement_paused
    if settlement_paused():
        logger.info("_update_results: settlement in PAUSA — nessun risultato "
                    "scaricato, nessuna riga chiusa")
        try:
            from tracker import get_results_stats
            stats = get_results_stats()
        except Exception:
            stats = {}
        return 0, stats, [], []
    # --- STEP 1: scarica risultati PRIMA di saldare ---
    # Refertazione ESCLUSIVAMENTE via the-odds-api (fetch_scores): la stessa
    # chiave delle quote restituisce anche i risultati FINITI delle partite
    # correnti. API-Football resta SOLO per lo storico ratings (football_hist,
    # stagioni 2022-2024 coperte dal piano free).
    # Refertazione MIRATA (risparmio crediti piano free): si interrogano SOLO
    # le leghe con scommesse attive (o chiuse da <48h) su partite già iniziate
    # — zero righe aperte = zero chiamate fetch_scores per quella lega.
    leagues = get_leagues_with_open_rows()
    # Risoluzione lega -> sport key: chiave SPORTS_MAP oppure alias/etichetta
    # (fix mapping leghe 11/09). Una lega non mappata NON viene saltata in
    # silenzio: senza sport key the-odds-api non puo' refertare, quindi logga.
    try:
        from sx_signals import league_to_sport as _league_to_sport
    except Exception:
        _league_to_sport = None
    # MISURA DEL COSTO (13/09): i crediti the-odds-api sono la risorsa piu'
    # scarsa del progetto, ma il loro consumo nel settlement non era mai
    # stato misurato (si leggeva solo il contatore globale). Logghiamo il
    # delta reale di questo giro: quanti crediti e quante leghe.
    try:
        from odds_api import get_remaining as _credits_left
        _cr_before = _credits_left()
    except Exception:
        _credits_left = None
        _cr_before = None
    updated = 0
    skipped_unmapped = 0
    for lg in leagues:
        sport = SPORTS_MAP.get(lg) or (_league_to_sport(lg) if _league_to_sport else None)
        if not sport:
            skipped_unmapped += 1
            logger.warning("_update_results: lega '%s' non mappata a uno sport "
                           "key the-odds-api — risultati non scaricati", lg)
            continue
        for m in fetch_scores(sport, days_from=SCORES_DAYS_FROM):
            if not m.get("id"):
                continue
            parsed = match_scores_by_name(m)
            if parsed is None:
                continue
            sh, sa = parsed
            save_result(m["id"], lg, m.get("home_team", ""), m.get("away_team", ""),
                        sh, sa, m.get("last_update", ""))
            updated += 1
    if _credits_left is not None and _cr_before is not None:
        try:
            _cr_after = _credits_left()
            logger.info(
                "settlement: %d leghe interrogate (%d non mappate, saltate), "
                "%d partite aggiornate, crediti %s -> %s (%d usati), politica %s",
                len(leagues) - skipped_unmapped, skipped_unmapped, updated,
                _cr_before, _cr_after, _cr_before - _cr_after,
                settlement_coverage_policy())
        except Exception:
            pass
    if updated:
        logger.info("Risultati scaricati: %d partite aggiornate.", updated)
    # --- STEP 2: salda cassa, previsioni e puntate AUTO ---
    try:
        settled = settle_cassa()
        if settled:
            logger.info("Cassa: saldate %d scommesse coi risultati reali.", settled)
    except Exception as e:
        logger.warning("settle_cassa fallita: %s", e)
    try:
        settled, pushes = settle_predictions()
        if settled:
            logger.info("Previsioni: saldate %d (di cui %d push).", settled, pushes)
    except Exception as e:
        logger.warning("settle_predictions fallita: %s", e)
    bet_settlements = []
    try:
        settled, pushes, details = settle_bets(return_details=True)
        bet_settlements = details
        if settled:
            logger.info("Puntate auto: saldate %d (di cui %d push).", settled, pushes)
    except Exception as e:
        logger.warning("settle_bets fallita: %s", e)
    # --- STEP 2.5: SANITY CHECK sui verdetti già emessi ---
    # Tripwire del bug 02/09 (punteggi invertiti): se match_results è stato
    # corretto dopo la chiusura (es. watchdog con match_scores_by_name), una
    # bet può restare 'won' con l'esito SPECCHIATO. Qui si ricomputa l'esito
    # dai gol correnti e si ri-saldano automaticamente le righe in
    # contraddizione, con alert su Telegram (blocca la chiusura sbagliata).
    sanity_alerts = []
    try:
        from tracker import settlement_sanity_check, heal_settled_contradictions
        contrad = settlement_sanity_check()
        if contrad:
            healed = heal_settled_contradictions(contrad)
            lines = []
            for c in contrad[:8]:
                lines.append(
                    f"   ⚠️ {c['table']} #{c['id']} {c['esito']}: "
                    f"era '{c['stored']}' → atteso '{c['expected']}' "
                    f"({c['home']} {c['sh']}-{c['sa']} {c['away']})")
            if len(contrad) > 8:
                lines.append(f"   … e altre {len(contrad) - 8} righe.")
            alert = ("🔔 *SANITY CHECK SETTLEMENT*\n"
                     f"{healed} verdetto/i in contraddizione coi gol "
                     "registrati: RI-SALDATI automaticamente\n\n" +
                     "\n".join(lines))
            sanity_alerts.append(alert)
            logger.warning("SANITY CHECK: %d righe ri-sal date "
                           "automaticamente", healed)
    except Exception as e:
        logger.warning("settlement sanity check fallita: %s", e)
    # --- STEP 3: aggiorna rating ---
    compute_ratings()
    return updated, get_results_stats(), bet_settlements, sanity_alerts


# --- Orario di inizio partita in ora ITALIANA (direttiva 02/10/2026) --------
def format_match_start(commence, *, prefix: str = "🕒 Inizio:") -> "str | None":
    """Riga "🕒 Inizio: HH:MM (IT)" dal timestamp UTC di inizio partita.

    Converte in `Europe/Rome` con la libreria standard (`zoneinfo`): l'ora
    legale e' gestita dal database tz, quindi la riga resta corretta anche
    dopo il cambio d'ora (a differenza dell'`IT_OFFSET` fisso dei job, che
    e' una scelta operativa diversa).

    Ritorna None se il timestamp manca o non e' parsabile: un orario
    INVENTATO in un messaggio operativo sarebbe peggio di nessuna riga
    (stessa regola del bankroll di avvio).
    """
    if not commence:
        return None
    try:
        dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Rome")
        except Exception:                 # fallback: offset fisso estivo IT
            tz = timezone(timedelta(hours=2))
        return f"{prefix} {dt.astimezone(tz).strftime('%H:%M')} (IT)"
    except Exception:
        return None


def _notify_enabled(env_name: str) -> bool:
    """True se la notifica Telegram `env_name` e' ATTIVA (default: spenta).

    Direttiva 02/10/2026: la Sandbox Tennis e il "Riepilogo di ieri" non
    inviano piu' messaggi su Telegram. Log, JSONL e DB restano intatti (la
    telemetria non si perde): cambia solo la consegna. Riattivabili senza
    toccare il codice con l'env corrispondente a 1.
    """
    return os.getenv(env_name, "0").strip().lower() in (
        "1", "true", "yes", "on")


def real_bankroll_usdc() -> "tuple[float | None, str]":
    """Bankroll REALE del conto per i messaggi di stato: (valore, base).

    Ordine di lettura (mai un valore inventato):
      1. EQUITY del wallet SX (`auto_bet._live_wallet_snapshot`): e' la
         STESSA fonte che governa Kelly, stop-loss e recinto d'esposizione
         — un messaggio che mostrasse un numero diverso da quello usato per
         dimensionare gli ordini sarebbe fuorviante;
      2. cassa del ledger (`adaptive_staking.bankroll_stats`), la base del
         percorso SIM;
      3. `(None, motivo)` — il chiamante DEVE dichiarare che il valore non
         e' disponibile. Il vecchio fallback fisso `100.00` faceva leggere
         un patrimonio che non esiste (direttiva 02/10/2026).
    """
    try:
        from auto_bet import _live_wallet_snapshot
        snap = _live_wallet_snapshot()
        if snap:
            equity = float(snap.get("equity") or 0.0)
            if equity > 0:
                return equity, (f"wallet SX — equity (disponibile "
                                f"{snap.get('available', 0.0):.2f} + in gioco "
                                f"{snap.get('exposure', 0.0):.2f})")
    except Exception as e:
        logger.warning("bankroll reale: lettura wallet SX fallita (%s)", e)
    try:
        from adaptive_staking import bankroll_stats
        current = float(bankroll_stats().get("current") or 0.0)
        if current > 0:
            return current, "cassa ledger"
    except Exception as e:
        logger.warning("bankroll reale: lettura cassa fallita (%s)", e)
    return None, "NON leggibile (wallet SX e cassa ledger non disponibili)"


def _admin_chat_ids() -> list:
    """Chat ID che ricevono SEMPRE i report (proprietario), anche senza /subscribe.

    Variabile ADMIN_CHAT_ID, separata da virgole se piu' di uno.
    """
    ids = []
    for part in os.getenv("ADMIN_CHAT_ID", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def _missing_env_keys() -> list:
    """Chiavi mancanti: i job corrispondenti saltano in silenzio."""
    missing = []
    for key, what in (("API_FOOTBALL_KEY", "storico ratings 2022-2024 (football_hist)"),
                      ("ODDS_API_KEY", "quote live + CLV + settlement risultati"),
                      ("QUOTAVERACE_BOT_TOKEN", "bot Telegram")):
        if not os.getenv(key):
            missing.append(f"{key} ({what})")
    return missing


def format_daily_report(since: str, label: str) -> str:
    """Riepilogo di un periodo (ISO `since`): previsioni chiuse per mercato,
    cassa saldata, CLV medio e job saltati per chiavi mancanti."""
    from tracker import predictions_summary, cassa_period, _get_conn

    by_mkt = predictions_summary(settled_since=since)
    ct = cassa_period(since)

    total_n = sum(b["n"] for b in by_mkt.values())
    total_pnl = sum((b["roi"] / 100.0) * b["n"] for b in by_mkt.values())
    total_ev = sum((b["avg_ev"] / 100.0) * b["n"] for b in by_mkt.values())

    lines = [f"📅 *RIEPILOGO — {label}*", "━━━━━━━━━━━━━━━━━━━━━━\n"]
    if total_n:
        lines.append(f"🎯 *Previsioni chiuse:* {total_n}")
        for mkt in ("1X2", "BTTS", "AH"):
            b = by_mkt.get(mkt)
            if not b or not b["n"]:
                continue
            outcome = f"✅ {b['won']}/❌ {b['lost']}"
            if b["push"]:
                outcome += f"/⚪ {b['push']}"
            lines.append(
                f"   {mkt}: {b['n']} ({outcome}) ROI {b['roi']:+.1f}% "
                f"vs EV {b['avg_ev']:+.1f}%"
            )
        closed = max(total_n - sum(b["push"] for b in by_mkt.values()), 1)
        hit = (sum(b["won"] for b in by_mkt.values()) / closed) * 100
        roi_tot = (total_pnl / total_n * 100) if total_n else 0.0
        lines.append(
            f"   *Totale: P/L {total_pnl:+.1f}u | ROI {roi_tot:+.1f}% "
            f"| hit {hit:.0f}%* (EV medio atteso {total_ev/total_n*100:+.1f}%)"
        )
    else:
        lines.append("🎯 *Nessuna previsione chiusa nel periodo.*")
        # Diagnostica: ci sono bet aperte ma senza risultati?
        try:
            from tracker import _get_conn
            conn = _get_conn(); c = conn.cursor()
            open_bets = c.execute(
                "SELECT COUNT(*) FROM bets WHERE esito_finale IS NULL"
            ).fetchone()[0]
            open_preds = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE esito_finale IS NULL"
            ).fetchone()[0]
            conn.close()
            if open_bets or open_preds:
                if not os.getenv("ODDS_API_KEY"):
                    lines.append(
                        "   ⚠️ *Possibile causa:* `ODDS_API_KEY` non configurata "
                        "→ i risultati non vengono scaricati e le bet "
                        "restano aperte. Impostala su Railway.")
                else:
                    lines.append(
                        f"   ℹ️ {open_bets} bet + {open_preds} previsioni "
                        "ancora aperte (risultati in attesa)")
        except Exception:
            pass

    if ct["chiusi"]:
        lines.append(
            f"💰 *Cassa saldata:* {ct['chiusi']} (✅ {ct['vinti']}/❌ {ct['persi']}) "
            f"| speso €{ct['speso']:.2f} | P/L €{ct['profit']:+.2f} | "
            f"ROI {ct['roi']:+.1f}%"
        )
    else:
        lines.append("💰 *Cassa:* nessuna puntata saldata nel periodo.")

    try:
        from market_calib import clv_vig_free, clv_raw
        conn = _get_conn(); c = conn.cursor()
        rows = c.execute("SELECT signal_quota, closing_quota, pinnacle_quota "
                         "FROM clv_history WHERE updated_at >= ?", (since,)).fetchall()
        conn.close()
        clvs_raw = []
        clvs_vf = []
        for s, clos, pin in rows:
            if not (clos and clos > 0 and s and s > 0):
                continue
            clvs_raw.append((s / clos) - 1.0)
            # Vig-free: usa Pinnacle come proxy fair (vig ~1-2%)
            fair = pin if (pin and pin > 0) else clos
            vf = clv_vig_free(s, fair)
            if vf is not None:
                clvs_vf.append(vf)
        if clvs_raw:
            lines.append(f"📈 *CLV medio:* {sum(clvs_raw)/len(clvs_raw)*100:+.2f}% (n {len(clvs_raw)})")
        if clvs_vf:
            lines.append(f"🎯 *CLV vig-free:* {sum(clvs_vf)/len(clvs_vf)*100:+.2f}% (n {len(clvs_vf)})")
        # CLV vs Pinnacle: la closing line piu' sharp del mercato.
        pin_clvs = [(s / pin) - 1.0 for s, _, pin in rows
                    if pin and pin > 0]
        if pin_clvs:
            lines.append(f"🏆 *CLV vs Pinnacle:* {sum(pin_clvs)/len(pin_clvs)*100:+.2f}% "
                         f"(n {len(pin_clvs)})")
    except Exception:
        pass

    # RLM / Steam / Crollo quota: movimenti di linea sui segnali attivi,
    # classificati dai VERI rilevatori (line_movement + rlm_alert) tramite
    # l'aggregatore condiviso con la webapp (/api/market_signals).
    try:
        from market_signals import collect_market_signals, format_market_signals_report
        lines.extend(format_market_signals_report(collect_market_signals()))
    except Exception as e:
        logger.warning("segnali mercato nel report falliti: %s", e)

    try:
        from tracker import bets_period
        bp = bets_period(since)
        if bp["piazzate"]:
            line = (f"🎯 *Puntate automatiche:* {bp['piazzate']} "
                    f"(€{bp['stake_totale']:.2f})")
            if bp["chiusi"]:
                outcome = f"✅ {bp['vinti']}/❌ {bp['persi']}"
                if bp["push"]:
                    outcome += f"/⚪ {bp['push']}"
                line += (f" | chiuse {bp['chiusi']} ({outcome}) "
                         f"P/L €{bp['profit']:+.2f}")
            lines.append(line)
    except Exception:
        pass

    # Scarti per liquidita' SX Bet (11/09): opportunita' non giocate perche'
    # il book era troppo sottile (segnale scartato o ordine saltato).
    # Diagnostica utile a tarare le soglie SX_MIN_*_USDC.
    try:
        from liquidity_monitor import summary as _liq_summary
        _liq = _liq_summary(days=1)
        if _liq["events"]:
            _k = ", ".join(f"{k}: {v}" for k, v in
                           sorted(_liq["by_kind"].items()))
            line = (f"💧 *Scarti liquidita' SX:* {_liq['events']} "
                    f"({_k})")
            if _liq["missed_profit"]:
                line += f" | edge perso ~{_liq['missed_profit']:.2f} USDC"
            lines.append(line)
    except Exception as e:
        logger.warning("monitor liquidita' nel report fallito: %s", e)

    # Flusso dell'order book SX (26/09): negli ultimi giri il book si e'
    # riempito in modo anomalo su qualche esito ("smart money"). TELEMETRIA:
    # nessun ordine parte da qui — e' il dato su cui decidere se collegarlo.
    try:
        from book_flow import summary as _bf_summary
        _bf = _bf_summary(days=1)
        if _bf["events"]:
            _r = ", ".join(f"{k}: {v}" for k, v in
                           sorted(_bf["by_reason"].items()))
            lines.append(f"📈 *Flusso book SX:* {_bf['events']} ingressi "
                         f"(+{_bf['total_delta_usdc']:.0f} USDC | {_r})")
    except Exception as e:
        logger.warning("flusso book nel report fallito: %s", e)

    # Audit qualita' dataset ML: un dataset sporco viene IMPARATO dal
    # modello come verita'. Controlla solo le previsioni/puntate chiuse
    # nel periodo e segnala i problemi (per tipo + primi esempi).
    try:
        from ml_dataset import build_training_rows
        from ml_audit import audit_training_rows, summarize
        period_rows = [r for r in build_training_rows()
                       if (r.get("settled_at") or "") >= since]
        problems = audit_training_rows(period_rows)
        if problems:
            by_type = summarize(problems)
            dettaglio = ", ".join(f"{t}: {n}" for t, n in by_type.items())
            lines.append(
                f"🔎 *Audit dataset ML:* ⚠️ {len(problems)} problemi "
                f"({dettaglio})"
            )
            for p in problems[:5]:
                # caratteri sicuri per il Markdown di Telegram
                msg = (p.get("msg") or "").replace("[", "(").replace("]", ")")
                lines.append(f"   • {p['tipo']} [{p.get('match_id','?')}] "
                             f"{p.get('mercato','?')} {p.get('esito','?')}: {msg}")
            if len(problems) > 5:
                lines.append(f"   … e altri {len(problems) - 5} problemi "
                             "(vedi `venv/bin/python ml_audit.py`)")
    except Exception as e:
        logger.warning("audit ML nel report fallito: %s", e)

    # Streak + bankroll/drawdown: il polso del periodo (stessi numeri del
    # backtest). Streak dalle previsioni chiuse, bankroll dalla cassa reale.
    try:
        from performance_report import _calc_streaks, _bankroll_stats
        conn = _get_conn(); c = conn.cursor()
        end = datetime.now().strftime("%Y-%m-%d")
        streaks = _calc_streaks(conn, since, end)
        br = _bankroll_stats(conn)
        conn.close()
        parts = []
        if streaks["current_streak"]:
            if streaks["current_type"] == "won":
                parts.append(f"🔥 {streaks['current_streak']} vittorie di fila")
            else:
                parts.append(f"📉 {streaks['current_streak']} perse di fila")
            parts.append(f"max {streaks['max_win_streak']}V/{streaks['max_loss_streak']}P")
        if br.get("current") is not None:
            parts.append(f"bankroll €{br['current']:.2f} "
                         f"(peak €{br['peak']:.2f}, dd {br['drawdown_pct']:.1f}%)")
        if parts:
            lines.append("📊 *Stato:* " + " | ".join(parts))
    except Exception as e:
        logger.warning("streak/bankroll nel report falliti: %s", e)

    # Concept drift del modello: Brier/LogLoss rolling vs baseline sulle
    # previsioni chiuse. Se la calibrazione sta degradando, il report
    # segnala il retraining dell'ensemble (monitoraggio periodico).
    try:
        from drift_monitor import check_drift, format_drift_report
        lines.extend(format_drift_report(check_drift()))
    except Exception as e:
        logger.warning("drift monitor nel report fallito: %s", e)

    missing = _missing_env_keys()
    if missing:
        lines.append("\n⚠️ *Job saltati per chiavi mancanti:*\n   " + "\n   ".join(missing))
    else:
        lines.append("\n✅ Tutti i job attivi (chiavi presenti).")

    # Partite trovate ma non analizzate per squadre fuori roster: gap di
    # copertura reso visibile (mai piu' silenzioso). Solo quelle delle
    # ultime 24h, cosi' il riepilogo del mattino segnala il giorno prima.
    try:
        from datetime import datetime as _dt
        from fixture_engine import get_skipped_matches
        def _recent(s):
            try:
                return (_dt.utcnow() - _dt.fromisoformat(s["ts"])).total_seconds() < 86400
            except Exception:
                return False
        skipped = [s for s in get_skipped_matches() if _recent(s)]
    except Exception:
        skipped = []
    if skipped:
        rows = "\n   ".join(
            f"• {s.get('home','?')} vs {s.get('away','?')} "
            f"[{s.get('league','?')}] — squadre non coperte: "
            f"{', '.join(s.get('non_coperte', []) or ['?'])}"
            for s in skipped[:5]
        )
        extra = f" (+{len(skipped)-5} altre)" if len(skipped) > 5 else ""
        lines.append(f"\n⚠️ *Partite non coperte (fuori roster):*\n   {rows}{extra}")
    return "\n".join(lines)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra il Chat ID: utile per impostare ADMIN_CHAT_ID nel .env."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"🆔 Il tuo Chat ID: `{chat_id}`\n\n"
        "Impostalo nel file `.env` (o su Railway) come:\n"
        f"`ADMIN_CHAT_ID={chat_id}`\n\n"
        "Così il bot ti invia SEMPRE i report mattutino e serale "
        "su Telegram, anche senza /subscribe.",
        parse_mode="Markdown")


async def cmd_riepilogo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Riepilogo del periodo: oggi (default), 'ieri' o una data YYYY-MM-DD."""
    from datetime import timedelta
    arg = " ".join(context.args or []).strip().lower()
    if arg in ("ieri", "yesterday"):
        since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        label = "IERI"
    elif arg:
        since = arg
        label = since
    else:
        since = datetime.now().strftime("%Y-%m-%d")
        label = "OGGI"
    text = format_daily_report(since, label)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from odds_api import get_quota
    q = get_quota()
    if q is None:
        text = ("🔋 *QUOTA the-odds-api*\n\n"
                "Nessuna scansione in cache.\n"
                "Fai `/analisi` per aggiornare i crediti.")
    else:
        remaining, n = q
        pct = remaining / 500 * 100
        text = (
            "🔋 *QUOTA the-odds-api*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚡ Crediti residui: **{remaining} / 500** ({pct:.0f}%)\n"
            f"📊 Campionati in cache: {n}/8\n\n"
            "📅 Reset: 1° del mese\n"
            "💡 Aggiornato con l'ultimo `/analisi` (costo zero)."
        )
    await update.message.reply_text(text + DISCLAIMER, parse_mode="Markdown")

async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = run_backtest()
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_backtest_mc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backtest_mc [sims] — Backtest walk-forward ML+Kelly con Monte Carlo.

    ROI atteso, Max Drawdown (base/mediana/p95) e probabilita' di riduzione,
    simulando lo stesso stack di produzione (ensemble + adaptive staking).
    """
    sims = 1000
    if context.args:
        try:
            sims = max(100, min(10000, int(context.args[0])))
        except ValueError:
            pass
    await update.message.reply_text(
        f"🔄 Backtest walk-forward + Monte Carlo ({sims} simulazioni)...\n"
        "Può richiedere qualche decina di secondi.")
    loop = asyncio.get_running_loop()
    try:
        from backtest_mc import run_backtest_mc, format_backtest_report
        res = await loop.run_in_executor(
            _scan_executor, run_backtest_mc, 30, sims, 100.0)
        text = format_backtest_report(res)
    except Exception as e:
        logger.error("cmd_backtest_mc: %s", e)
        text = f"❌ Errore backtest MC: {e}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_autobet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/autobet [off|stop|sim|pause|live|resume] — kill-switch delle
    puntate automatiche (solo admin).

    - `/autobet` o `/autobet status`: stato attuale;
    - `/autobet off` (o `stop`): STOP TOTALE — nessuna puntata (ne' reale
      ne' simulata) finche' non si riattiva;
    - `/autobet sim` (o `pause`): PAUSA ordini reali — torna al paper
      trading;
    - `/autobet live` (o `resume`/`on`): ripristina AUTO_BET_MODE env;
    - `/autobet now`: esegue SUBITO il giro di puntate del giorno (LIVE
      se configurato) senza aspettare il job delle 08:50.

    L'override e' persistente (data/execution/auto_bet_mode.json, volume
    condiviso): sopravvive ai redeploy ed e' letto da auto_bet a ogni giro.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    from auto_bet import (clear_kill_switch, kill_switch_status,
                          set_kill_switch)
    arg = (context.args[0] if context.args else "").strip().lower()
    try:
        if arg in ("off", "stop"):
            set_kill_switch("off")
            await update.message.reply_text(
                "🛑 *AUTO-BET: STOP TOTALE*\n\n"
                "Nessuna puntata (reale o simulata) finche' non riattivi.\n"
                "Per riattivare: `/autobet live`", parse_mode="Markdown")
        elif arg in ("sim", "pause"):
            set_kill_switch("sim")
            await update.message.reply_text(
                "⏸️ *AUTO-BET: PAUSA ordini reali*\n\n"
                "Resta attivo il paper trading (SIM).\n"
                "Per riattivare i reali: `/autobet live`",
                parse_mode="Markdown")
        elif arg in ("live", "resume", "on"):
            clear_kill_switch()
            await update.message.reply_text(
                "▶️ *AUTO-BET: riattivato*\n\n"
                "Torna a seguire `AUTO_BET_MODE` env. Usa `/autobet` per "
                "verificare lo stato.", parse_mode="Markdown")
        elif arg == "now":
            await update.message.reply_text(
                "🚀 Esecuzione immediata del giro puntate "
                "(LIVE se configurato)...")
            loop = asyncio.get_running_loop()
            try:
                from auto_bet import run_today_bets
                placed = await loop.run_in_executor(
                    _scan_executor, run_today_bets, None, True)
            except Exception as e:
                logger.error("cmd_autobet now: %s", e)
                await update.message.reply_text(f"❌ Errore esecuzione: {e}")
                return
            if not placed:
                await update.message.reply_text(
                    "ℹ️ Nessuna puntata piazzata (nessun segnale oggi, "
                    "kill-switch attivo o mercati non disponibili). "
                    "Usa `/autobet` per lo stato.", parse_mode="Markdown")
                return
            mode = placed[0]["mode"]
            mode_label = {"live": "LIVE", "sim": "SIMULAZIONE"}.get(
                mode, mode)
            total = sum(p["stake"] for p in placed)
            rows = "\n".join(
                f"• {p['home']} vs {p['away']} — {p['esito_key']} @ "
                f"{p['price']:.2f} (€{p['stake']:.2f})" for p in placed)
            await update.message.reply_text(
                f"🎯 *PUNTATE AUTOMATICHE ({mode_label})*\n"
                f"{len(placed)} puntate, €{total:.2f} di stake\n\n"
                f"{rows}\n\n"
                f"📌 {'ORDINI REALI' if mode == 'live' else 'Simulazione: nessun ordine reale inviato.'}",
                parse_mode="Markdown")
        else:
            st = kill_switch_status()
            mode_label = {"off": "🛑 OFF (nessuna puntata)",
                          "sim": "🟡 SIM (paper trading)",
                          "live": "🟢 LIVE (ordini reali)"}
            ov_label = {"off": "🛑 STOP TOTALE",
                        "sim": "⏸️ PAUSA ordini reali"}.get(
                            st["override"], "nessuno (env)")
            # Cap severo (11/09): con il cap vincolante una bet il cui stake
            # cappato e' sotto il minimo ordine exchange viene saltata. Se il
            # wallet e' troppo piccolo per rispettare il cap, l'admin deve
            # saperlo SUBITO (altrimenti sembra che il bot non funzioni).
            from auto_bet import (MIN_STAKE_EUR, cap_hard_active,
                                   _live_wallet_snapshot, daily_stop_status,
                                   weekly_stop_status)
            cap_line = ("✅ attivo (il floor exchange non alza lo stake)"
                        if cap_hard_active() else
                        "❌ disattivato (vale il floor exchange)")
            _ds = daily_stop_status()
            stop_line = (f"🛑 *ATTIVO* fino a {str(_ds.get('until'))[:16]} "
                         f"(perdita ≥ {_ds['loss_pct']:.0f}% giornaliera)"
                         if _ds.get("stopped") else
                         f"🟢 non attivo (soglia -{_ds['loss_pct']:.0f}%)")
            _ws = weekly_stop_status()
            weekly_line = (
                f"🛑 *ATTIVO* fino a {str(_ws.get('until'))[:16]} "
                f"(drawdown -{(_ws.get('drawdown_pct') or 0):.1f}% "
                f"rolling {_ws['window_h']:.0f}h, soglia -{_ws['loss_pct']:.0f}%)"
                if _ws.get("stopped") else
                f"🟢 non attivo (soglia -{_ws['loss_pct']:.0f}% "
                f"rolling {_ws['window_h']:.0f}h)")
            # Il cap si misura sull'EQUITY (disponibile + in gioco): e' lo
            # stesso valore che usa lo staking, cosi' l'operatore non legge
            # due bankroll diversi (fix 15/09).
            wallet_warn = ""
            try:
                snap = _live_wallet_snapshot()
                if snap:
                    wallet_warn = (
                        f"• Wallet: {snap['equity']:.2f} USDC equity "
                        f"({snap['available']:.2f} liberi + "
                        f"{snap['exposure']:.2f} in gioco)\n")
                    if cap_hard_active() and \
                            snap["equity"] * 0.01 < MIN_STAKE_EUR:
                        wallet_warn += (
                            "\n⚠️ *Cap severo non sostenibile col saldo "
                            "attuale:* "
                            f"{snap['equity']:.2f} USDC → il cap 1% "
                            f"({snap['equity'] * 0.01:.2f}) è sotto "
                            f"il minimo ordine ({MIN_STAKE_EUR:.2f} USDC): "
                            "*nessun ordine verrà piazzato* finché il wallet "
                            "non arriva a ~100 USDC (o 50 USDC per il cap "
                            "2%).\n")
            except Exception:
                pass
            text = (
                "🎛 *AUTO-BET — stato*\n\n"
                f"• Esecuzione effettiva: "
                f"*{mode_label.get(st['effective'], st['effective'])}*\n"
                f"• Override kill-switch: {ov_label}\n"
                f"• AUTO_BET_MODE env: `{st['env_mode'] or 'sim'}`\n"
                f"• Provider reale pronto: "
                f"{'✅ sì' if st['provider_ready'] else '❌ no'}\n"
                f"• Cap per bet: 1% value/moderate · 2% strong_value\n"
                f"• Cap severo: {cap_line}\n"
                f"• Stop-loss giornaliero: {stop_line}\n"
                + ("  ↳ per azzerarlo subito: `/stopreset`\n"
                   if _ds.get("stopped") else "")
                + f"• Circuit breaker settimanale: {weekly_line}\n"
                + ("  ↳ per azzerarlo subito: `/weeklyreset`\n"
                   if _ws.get("stopped") else "")
                + f"{wallet_warn}\n"
                "Comandi:\n"
                "`/autobet off` – stop totale\n"
                "`/autobet sim` – pausa ordini reali\n"
                "`/autobet live` – riattiva")
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_autobet: %s", e)
        await update.message.reply_text(f"❌ Errore: {e}")


async def cmd_settlement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settlement [on|off|stato] — pausa del settlement automatico (admin).

    - `/settlement` o `/settlement stato`: stato attuale;
    - `/settlement off`: PAUSA il settlement (nessuna chiusura automatica di
      bet/previsioni/cassa e nessun download risultati);
    - `/settlement on`: riattiva il settlement.

    L'override e' persistente (data/execution/settlement_paused.json, volume
    condiviso) e ha precedenza; la env `SETTLEMENT_PAUSED=1` lo attiva in
    modo fail-safe (la rimozione del file non basta finche' resta a 1).
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    from tracker import (settlement_pause_status, set_settlement_paused)
    arg = (context.args[0] if context.args else "").strip().lower()
    try:
        if arg in ("off", "stop", "pause", "pausa"):
            set_settlement_paused(True)
            await update.message.reply_text(
                "🛑 *SETTLEMENT IN PAUSA*\n\n"
                "Nessuna chiusura automatica di bet/previsioni/cassa e "
                "nessun download risultati.\n"
                "Per riattivare: `/settlement on`", parse_mode="Markdown")
        elif arg in ("on", "start", "resume", "riattiva"):
            set_settlement_paused(False)
            await update.message.reply_text(
                "▶️ *SETTLEMENT RIATTIVATO*\n\n"
                "Le prossime chiusure automatiche tornano attive. "
                "Usa `/settlement` per lo stato.", parse_mode="Markdown")
        else:
            st = settlement_pause_status()
            state = ("🛑 IN PAUSA" if st["paused"] else "🟢 ATTIVO")
            extra = ("\n⚠️ Env `SETTLEMENT_PAUSED=1` impostata: la pausa "
                     "resta attiva finche' non la togli su Railway."
                     if st.get("env") else "")
            await update.message.reply_text(
                "⚙️ *SETTLEMENT — stato*\n\n"
                f"• Settlement automatico: {state}\n"
                f"• File override: `{st['file']}`{extra}\n\n"
                "Comandi:\n"
                "`/settlement off` – pausa\n"
                "`/settlement on` – riattiva",
                parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_settlement: %s", e)
        await update.message.reply_text(f"❌ Errore: {e}")


# ---------------------------------------------------------------------------
# Revisioni umane su Telegram (15/09/2026)
# ---------------------------------------------------------------------------
# La catena di decisione (`decision/`) mette in coda i verdetti `review`: qui
# c'e' il pezzo che li rende azionabili — il prompt con i bottoni e il callback
# IDEMPOTENTE che lo chiude. Due click, un redelivery di Telegram o un redeploy
# a meta' lavoro producono UNA decisione (`decision/review_telegram.py`).
# In questa fase l'approvazione NON esegue: i gateway sono shadow, quindi il
# click attraversa la catena e REGISTRA l'ordine che sarebbe partito.

REVIEW_JOB_INTERVAL_SECONDS = 300
REVIEW_JOB_PROMPTS_PER_GIRO = 5


def _review_queue_and_store():
    """Coda + store dei callback (path dal volume, override da env)."""
    from decision.review_queue import ReviewQueue
    from decision.review_telegram import CallbackStore
    return ReviewQueue(), CallbackStore()


def _send_review_prompts_pass() -> dict:
    """Bloccante: invia i prompt delle revisioni in attesa (per l'executor)."""
    from decision.middleware import Observability
    from decision.review_telegram import send_prompts
    queue, store = _review_queue_and_store()
    obs = Observability(component="bot.review")
    return send_prompts(queue=queue, store=store,
                        limit=REVIEW_JOB_PROMPTS_PER_GIRO, observability=obs)


def _review_callback_pass(payload: dict) -> dict:
    """Bloccante: chiude il callback e risponde a Telegram (per l'executor)."""
    from decision.middleware import Observability
    from decision.review_telegram import answer_callback
    queue, store = _review_queue_and_store()
    obs = Observability(component="bot.review")
    try:
        from auto_bet import _execution_mode
        mode = _execution_mode()
    except Exception:
        mode = "sim"
    outcome = answer_callback(payload, queue=queue, store=store, observability=obs,
                              mode=mode if mode in ("live", "sim") else "sim")
    return outcome.as_dict()


async def review_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bottone ✅ Approva / ❌ Rifiuta di un verdetto REVIEW (solo admin).

    Si risponde SEMPRE alla callback query (anche sui duplicati): e' la
    risposta che ferma i redelivery di Telegram. L'idempotenza sta nello store
    (`decision/review_telegram.py`), non nel tempo di risposta.
    """
    query = update.callback_query
    if query is None:
        return
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat and update.effective_chat.id not in admin_ids:
        try:
            await query.answer("⛔ Comando riservato agli admin.", show_alert=True)
        except Exception:
            pass
        return
    sender = getattr(query, "from_user", None)
    message = getattr(query, "message", None)
    payload = {
        "id": query.id,
        "data": query.data,
        "from": {"id": getattr(sender, "id", None),
                 "username": getattr(sender, "username", None),
                 "first_name": getattr(sender, "first_name", None)},
        "message": {
            "message_id": getattr(message, "message_id", None),
            "text": getattr(message, "text", "") or "",
            "chat": {"id": getattr(getattr(message, "chat", None), "id", None)},
        },
    }
    try:
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(_scan_executor, _review_callback_pass, payload)
    except Exception as exc:                      # mai far cadere l'handler
        logger.error("review callback: %s", exc)
        try:
            await query.answer("Errore interno: riprova.", show_alert=True)
        except Exception:
            pass
        return
    logger.info("review callback: %s -> %s%s (revisione %s)",
                outcome.get("action"), outcome.get("status"),
                " [duplicato]" if outcome.get("duplicate") else "",
                outcome.get("record_id") or "-")
    if outcome.get("would_order"):
        logger.info("review: ordine che SAREBBE partito (shadow) — stake %s, "
                    "comandi %s", outcome.get("stake"), outcome.get("commands"))


async def decision_review_job(context: ContextTypes.DEFAULT_TYPE = None) -> None:
    """Invia i prompt delle revisioni in attesa (ogni 5 min, admin).

    Il marker di prompt inviato sulla store rende l'invio idempotente: lo
    stesso segnale non viene rimandato a ogni giro. Zero costi API (nessuna
    rete oltre a Telegram).
    """
    try:
        from decision.shadow import reviews_enabled
        if not reviews_enabled():
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_scan_executor, _send_review_prompts_pass)
        if result.get("sent"):
            logger.info("review: %d prompt revisioni inviati", result["sent"])
        for error in result.get("errors") or []:
            logger.warning("review: invio fallito (%s)", error)
    except Exception as exc:
        logger.warning("decision_review_job: %s", exc)


async def cmd_revisioni(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/revisioni — stato delle revisioni umane (admin).

    Mostra i verdetti `review` in attesa con le loro chiavi di callback (usabili
    anche da CLI: `venv/bin/python -m decision review --callback <chiave>`) e il
    riepilogo dei callback gia' gestiti.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    try:
        from decision.review_telegram import (format_report as review_report,
                                              pending_prompts)
        queue, store = _review_queue_and_store()
        prompts = pending_prompts(queue, store, include_prompted=True)
        lines = [review_report(store=store, queue=queue)]
        if prompts:
            lines.append(f"\n  prompt (con chiave per la CLI):")
            for prompt in prompts[:5]:
                lines.append(
                    f"    · {prompt['entry'].get('selection')} "
                    f"@ {prompt['entry'].get('price')} → "
                    f"`{prompt['callback_ids']['approve']}`")
        else:
            lines.append("\n  nessuna revisione in attesa")
        lines.append("\n  Approva/rifiuta dai bottoni del prompt, oppure:")
        lines.append("  `venv/bin/python -m decision review --callback <chiave>`")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.error("cmd_revisioni: %s", exc)
        await update.message.reply_text(f"❌ Errore: {exc}")


async def cmd_sxscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sxscan — giro immediato dello scan SX Bet + stato settlement (admin).

    Stesso lavoro di sx_signals_job ma on-demand: scan dei mercati 1X2
    calcio sull'order book SX (API pubblica), salvataggio dei segnali value
    nel ledger (l'auto-bet li piazza al giro successivo) e settlement delle
    bet sx-* aperte.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    await update.message.reply_text("📡 Scan SX Bet in corso (sola lettura)...")
    loop = asyncio.get_running_loop()

    def _pass():
        from sx_signals import scan, settle_sx_bets
        return scan(), settle_sx_bets()

    try:
        signals, settle_res = await loop.run_in_executor(_scan_executor, _pass)
    except Exception as e:
        logger.error("cmd_sxscan: %s", e)
        await update.message.reply_text(f"❌ Errore scan SX: {e}")
        return
    text = f"📡 *SCAN SX BET* — {len(signals)} segnali value salvati"
    if signals:
        rows = "\n".join(
            f"• {s['home']} vs {s['away']} — {s['esito']} @ {s['quota']:.2f} "
            f"(EV {s['ev'] * 100:+.1f}%) [{s['status']}]"
            for s in signals[:10])
        text += f"\n\n{rows}"
    if settle_res.get("open"):
        text += (f"\n\n💰 Bet SX aperte: {settle_res['open']} | saldate ora: "
                 f"{settle_res.get('settled', 0)} "
                 f"(fonte: {settle_res.get('source') or 'nessuna'})")
    text += "\n\n💡 Il giro auto-bet le piazza al prossimo tick (1 min)."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — snapshot manuale del DB + dataset ML (solo admin)."""
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text(
            "⛔ Comando riservato agli admin.")
        return
    await update.message.reply_text("💾 Backup in corso...")
    loop = asyncio.get_running_loop()
    try:
        from backup_manager import run_backup, format_backup_report
        s = await loop.run_in_executor(_scan_executor, run_backup)
        text = format_backup_report(s)
    except Exception as e:
        logger.error("cmd_backup: %s", e)
        text = f"❌ Errore backup: {e}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Sincronizzazione risultati storici... (può richiedere qualche minuto per via del rate limit)", parse_mode="Markdown")
    text = run_sync()
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_hedge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hedge — stato della copertura intelligente (solo admin)."""
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text(
            "⛔ Comando riservato agli admin.")
        return
    try:
        from smart_hedging import format_report
        await update.message.reply_text(format_report())
    except Exception as e:
        logger.error("cmd_hedge: %s", e)
        await update.message.reply_text(f"❌ Errore hedge: {e}")

async def cmd_ordini(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ordini — verifica ordini reali: stake fisso e recinto (solo admin).

    Direttiva 28/09/2026: stake fisso 1.50 USDC e recinto del 40%
    sull'esposizione APERTA. Il controllo e' una lettura del ledger (zero
    crediti) e dichiara anche cio' che NON ha potuto verificare.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text(
            "⛔ Comando riservato agli admin.")
        return
    try:
        import order_watch
        await update.message.reply_text(order_watch.format_report())
    except Exception as e:
        logger.error("cmd_ordini: %s", e)
        await update.message.reply_text(f"❌ Errore verifica ordini: {e}")


def format_bet_verdicts(settlements: list) -> str:
    """Formatta i verdetti delle puntate appena saldate (fine partita)."""
    if not settlements:
        return ""
    mode_txt = "DRY-RUN" if settlements[0].get("mode") == "dry-run" else "LIVE"
    lines = []
    for s in settlements:
        if s["outcome"] == "won":
            icon = "✅"
            verdict = "VINTA"
        elif s["outcome"] == "push":
            icon = "⚪"
            verdict = "PUSH"
        else:
            icon = "❌"
            verdict = "PERSA"
        profit = s["profit"] or 0.0
        pl = f"+€{profit:.2f}" if profit >= 0 else f"-€{abs(profit):.2f}"
        lines.append(
            f"{icon} *{verdict}* — {s.get('home', '?')} vs {s.get('away', '?')}"
            f" ({s.get('league', '')})\n"
            f"   🎯 {s['mercato']} {s['esito']} @ {s['price']:.2f} | "
            f"stake €{s['stake']:.2f} | *P/L {pl}*"
        )
    return ("🔔 *ESITO PUNTATE AUTOMATICHE* "
            f"({mode_txt})\n\n" + "\n\n".join(lines))


async def _send_bet_settlements(context, settlements: list):
    text = format_bet_verdicts(settlements)
    if text:
        await _send_report_to_recipients(context, text)


async def cmd_risultati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        from tracker import get_results_stats
        updated, stats, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
        if stats["total"] == 0:
            text = "📊 *RISULTATI TRACKING*\n\nNessuna scommessa chiusa ancora.\nI risultati si aggiornano da soli quando le partite finiscono."
        else:
            text = (
                "📊 *RISULTATI TRACKING*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 Scommesse chiuse: {stats['total']}\n"
                f"✅ Vinte: {stats['won']} | ❌ Perse: {stats['lost']}\n"
                f"📈 Hit rate: {stats['hit_rate']:.1f}%\n"
                f"💰 P/L (unità da 1): {stats['net']:+.2f}\n"
                f"📊 ROI: {stats['roi']:+.2f}%\n"
                f"⚖ EV medio segnali: {stats['avg_ev']*100:+.2f}%\n"
            )
            clv = stats.get("avg_clv", 0.0)
            clv_tracked = stats.get("clv_tracked", 0)
            if clv_tracked:
                clv_txt = f"+{clv*100:.2f}%" if clv >= 0 else f"{clv*100:.2f}%"
                clv_line = (
                    f"\n🎯 *Closing Line Value* (su {clv_tracked} segnali): {clv_txt}\n"
                    f"   > 0 significa che battiamo la chiusura del mercato 👉 edge reale"
                )
                text += clv_line
        if updated:
            text += f"\n\n🔄 Aggiornate {updated} partite dai risultati."
        try:
            from tracker import cassa_totals
            ct = cassa_totals()
            if ct["chiusi"]:
                text += (
                    "\n\n💰 *CASSA REALE* (le tue puntate)\n"
                    f"   Chiuse: {ct['chiusi']} (✅ {ct['vinti']} / ❌ {ct['persi']}) "
                    f"| in gioco: {ct['in_gioco']}\n"
                    f"   Speso: €{ct['totale_speso']:.2f} | "
                    f"P/L: €{ct['profit_realizzato']:+.2f} | ROI: {ct['roi']:+.2f}%"
                )
        except Exception:
            pass
        # Telemetria di calibrazione: torto/ragione per MERCATO. E' qui che
        # si vede se il modello batte davvero la closing line, per mercato.
        try:
            from tracker import predictions_summary
            by_mkt = predictions_summary()
            lines = []
            for mkt in ("1X2", "OU", "BTTS", "AH"):
                b = by_mkt.get(mkt)
                if not b or not b["n"]:
                    continue
                outcome = f"✅ {b['won']}/❌ {b['lost']}"
                if b["push"]:
                    outcome += f"/⚪ {b['push']}"
                lines.append(
                    f"   {mkt}: {b['n']} prev ({outcome}) "
                    f"ROI {b['roi']:+.1f}% vs EV {b['avg_ev']:+.1f}%"
                )
            if lines:
                text += "\n\n📊 *CALIBRAZIONE PER MERCATO*\n" + "\n".join(lines)
        except Exception:
            pass
        await update.message.reply_text(text + DISCLAIMER, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Errore risultati: {e}")
        await update.message.reply_text("❌ Errore nel recupero risultati.", parse_mode="Markdown")

# Ritardo dei segnali per il piano free (ore): il premium li riceve subito.
FREE_DELAY_HOURS = 3

async def notify_job(context: ContextTypes.DEFAULT_TYPE, delayed: bool = False):
    """Notifica value bet: immediata per premium, ritardata per free.

    delayed=True e' usato dal job free (3 ore dopo): ripete il controllo ma
    salta i segnali gia' spediti ai premium (mark_notified e' globale).
    """
    if not os.getenv("ODDS_API_KEY") or not LIVE_ODDS_AVAILABLE: return
    try:
        odds = get_live_odds()
        if not odds: return
        value_signals = filter_value_bets(enrich_odds_with_probs(odds), ev_threshold=0.03)
        if not value_signals: return
        if delayed:
            # Piano free: solo segnali non ancora notificati (i premium li
            # hanno gia' ricevuti al giro immediato, con mark_notified).
            subscribers = [cid for cid in get_subscribers(tier="free")
                           if not is_premium(cid)]
        else:
            subscribers = [cid for cid in get_subscribers()
                           if is_premium(cid)]
        if not subscribers: return
        today = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
        if not delayed:
            # Sticker animato una volta per chat, prima dei messaggi premium.
            for chat_id in subscribers:
                await send_premium_sticker(context.bot, chat_id)
        for sig in value_signals[:3]:
            if is_notified(sig.get("match_id","unknown"), today): continue
            ev_pct = sig["ev"] * 100
            prob = sig.get("probabilita", 0)
            quota = sig.get("quota_decimale", 1.0)
            pro = get_pro_stake(100.0, prob, quota)
            tag = "💎 PREMIUM" if not delayed else "🔔 VALUE BET"
            msg = (
                f"🔔 *NOTIFICA {tag}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🏟 {sig['evento']}\n"
                f"🎯 {sig['esito']} @ {sig['quota_decimale']:.2f} ({sig['bookmaker']})\n"
                f"📈 EV: +{ev_pct:.2f}% | Stake ref: €{pro['stake']:.2f} ({pro['stake_pct_of_bankroll']:.1f}%)\n\n"
                f"🛡 Filtri: {FILTRI_TXT}\n\n"
                f"💡 `/segnale` per analisi dettagliata"
            )
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    # Traccia il segnale ricevuto: alimenta /storico_personale,
                    # il backtest e il tracking risultati/CLV.
                    log_signal(chat_id, sig["evento"], sig["esito"],
                               sig["quota_decimale"], prob, sig["ev"])
                except Exception:
                    pass
            mark_notified(sig.get("match_id","unknown"), today)
    except Exception as e: logger.error(f"Errore notify: {e}")

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job mattutino: calendario + schedina Pro")
    fetch_and_analyze_today()
    picks = get_value_picks_for_schedina()
    if not picks: return
    text = format_schedina(picks, 100.0)
    await _send_report_to_recipients(context, text)

async def afternoon_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job pomeridiano: ricontrollo Pro")
    fetch_and_analyze_today()
    try:
        _, _, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore update risultati job: {e}")
    await notify_job(context)          # immediato per i premium
    await notify_job(context, delayed=True)  # ritardo 3h per il piano free

async def free_delayed_job(context: ContextTypes.DEFAULT_TYPE):
    """Job con ritardo di 3 ore per il piano free."""
    await notify_job(context, delayed=True)

async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job serale: ricontrollo Pro")
    fetch_and_analyze_today()
    try:
        _, _, settlements, sanity = _update_results()
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore update risultati job: {e}")
    await notify_job(context)

async def results_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Job risultati serali (23:30 ITA)")
    try:
        updated, stats, settlements, sanity = _update_results()
        logger.info(f"Risultati aggiornati: {updated} partite")
        if settlements:
            await _send_bet_settlements(context, settlements)
        for alert in sanity:
            await _send_report_to_recipients(context, alert)
    except Exception as e:
        logger.error(f"Errore results_job: {e}")


async def settlement_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    """Self-healing pendenze: ogni 4h (dopo la finestra dei match job)
    scarica i risultati e salda cassa/previsioni/puntate rimaste aperte.

    Copre i buchi della copertura job (es. redeploy alle 23:13 che salta il
    results_job delle 21:30, come il 01/09): al prossimo tick le bet delle
    16:40 vengono saldate automaticamente, senza intervento manuale.
    Frequenza 4h (era 2h): il referto non serve istantaneo per il ROI e
    ogni chiamata in meno aiuta il budget crediti del piano free.
    """
    try:
        updated, stats, settlements, sanity = _update_results()
    except Exception as e:
        logger.error(f"Errore settlement_watchdog_job: {e}")
        return
    open_bets = open_preds = -1
    try:
        from tracker import _get_conn
        conn = _get_conn(); c = conn.cursor()
        open_bets = c.execute(
            "SELECT COUNT(*) FROM bets WHERE esito_finale IS NULL").fetchone()[0]
        open_preds = c.execute(
            "SELECT COUNT(*) FROM predictions WHERE esito_finale IS NULL").fetchone()[0]
        # Timeout per puntate LIVE >6h senza settlement
        timeout_6h = 0
        try:
            timeout_6h = c.execute(
                "SELECT COUNT(*) FROM bets WHERE mode='live' "
                "AND esito_finale IS NULL AND settled_at IS NULL "
                "AND datetime('now') > datetime(created_at, '+6 hours')"
            ).fetchone()[0]
        except Exception:
            pass
        conn.close()
    except Exception:
        timeout_6h = 0
        pass
    if updated or settlements or sanity:
        logger.info("settlement_watchdog: %d risultati, %d bet saldate, "
                    "pendenze: %d bet / %d previsioni, %d sanity check",
                    updated, len(settlements), open_bets, open_preds,
                    len(sanity))
    if timeout_6h > 0:
        logger.warning("settlement_watchdog: %d puntate LIVE senza settlement >6h",
                       timeout_6h)
    if settlements:
        await _send_bet_settlements(context, settlements)
    for alert in sanity:
        await _send_report_to_recipients(context, alert)

async def retrain_ensemble_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Addestra e salva l'ensemble ML sul volume dal ledger live (daily).

    Attiva il ML in produzione: build_training_rows() legge predictions+bets
    chiuse dal DB (zero chiamate API), e se il dataset supera MIN_SAMPLES
    addestra XGBoost (o Logistic fallback) e salva data/ensemble_model.json
    sul volume. Prima di questo job il modello non esisteva in produzione:
    get_ensemble() restituiva un ensemble NON addestrato e le analisi usavano
    solo Poisson+blend (ml_available=False). Dopo il retrain azzera la cache
    del singleton cosi' la prossima analisi carica il modello nuovo.

    Se il dataset e' ancora troppo piccolo esce senza effetti (log INFO).
    """
    try:
        from ml_ensemble import train_ensemble, reset_ensemble_cache, MIN_SAMPLES
        metrics = train_ensemble()
        status = metrics.get("status")
        if status == "trained":
            reset_ensemble_cache()
            logger.info(
                "Ensemble ML riaddestrato e salvato: n=%s, brier=%.4f, "
                "acc=%.3f, type=%s", metrics.get("n_samples"),
                metrics.get("brier_score", 0.0), metrics.get("accuracy", 0.0),
                metrics.get("model_type"))
        elif status == "insufficient_data":
            logger.info("Ensemble ML: dataset insufficiente (%s/%s righe), "
                        "riprovo al prossimo giro", metrics.get("n"), MIN_SAMPLES)
        else:
            logger.warning("Ensemble ML retrain: %s", metrics)
    except Exception as e:
        logger.warning("Retrain ensemble fallito: %s", e)


async def _send_report_to_recipients(context, text: str):
    """Invia il messaggio agli iscritti + sempre ai chat ADMIN_CHAT_ID."""
    chat_ids = set(get_subscribers())
    chat_ids.update(_admin_chat_ids())
    for chat_id in sorted(chat_ids):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode="Markdown")
        except Exception:
            pass


# Stato dell'ultimo alert drift (processo): evita di ripetere l'allerta
# ogni 6h finche' il drift persiste. Alerta solo al passaggio a "drift"
# oppure se l'ultimo alert risale a >24h (ricordo periodico).
_DRIFT_LAST_ALERT: dict = {}


async def drift_watchdog_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Monitoraggio CONTINUO del drift in background (ogni 6h).

    A differenza della sezione 🧠 del report giornaliero (che mostra lo
    stato una volta al giorno), questo job controlla la calibrazione
    rolling dell'ensemble con cadenza regolare e allerta admin + iscritti
    SOLO quando il drift e' rilevato (status="drift"), con anti-spam:
    niente messaggi quando lo stato e' ok/insufficiente, e al massimo un
    alert ogni 24h se il drift persiste. Il retraining vero e' gia'
    schedulato (05:45 UTC + boot): l'alert serve da campanello, non da
    azione. Lo stato viene comunque loggato a ogni giro.
    """
    try:
        from drift_monitor import check_drift, format_drift_report
        d = check_drift()
        status = d.get("status")
        logger.info("drift_watchdog: status=%s n=%s (rolling %s vs baseline %s)",
                    status, d.get("n"), d.get("brier_rolling"),
                    d.get("brier_baseline"))
        if status != "drift":
            _DRIFT_LAST_ALERT["status"] = status
            return
        now = datetime.now().timestamp()
        last_ts = _DRIFT_LAST_ALERT.get("ts", 0.0)
        if _DRIFT_LAST_ALERT.get("status") == "drift" and now - last_ts < 86400:
            return  # gia' segnalato nelle ultime 24h
        _DRIFT_LAST_ALERT.update(status="drift", ts=now)
        lines = ["🔔 *DRIFT MODELLO — monitoraggio automatico*"]
        lines.extend(format_drift_report(d))
        lines.append("Il retraining e' schedulato (05:45 UTC + boot); "
                     "se non si e' ancora risolto, verificare i prossimi "
                     "settlement.")
        text = "\n".join(lines)
        if context is not None:
            await _send_report_to_recipients(context, text)
        else:
            logger.warning("drift_watchdog: %s", " | ".join(lines))
    except Exception as e:
        logger.warning("drift_watchdog fallito: %s", e)


async def credit_watchdog_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Monitoraggio crediti the-odds-api ogni 6h.

    Due campanelli, perche' il livello da solo non basta:
    1) soglie assolute (50/20/10/5) sul residuo — avvisano quando e' tardi;
    2) RITMO di consumo misurato (`odds_api.credit_budget_status`, finestra
       48h) — dice se il budget regge fino al reset. Il 15/09 il residuo di
       273 crediti sembrava tranquillo, ma a 58/giorno finiva il 20/09 (tre
       settimane prima del reset): il ritmo lo vede, la soglia no.
    """
    from odds_api import get_quota
    try:
        quota = get_quota()
        if quota is None:
            logger.warning("credit_watchdog: unable to read credit cache")
            return
        remaining, n_sports = quota
        logger.info("credit_watchdog: remaining=%d (%d sports cached)",
                    remaining, n_sports)

        # Alert thresholds
        if remaining <= 5:
            text = f"\u26a0\ufe0f **CREDITI CRITICI**\n\n" \
                   f"Il piano the-odds-api ha solo **{remaining}** crediti rimasti.\n" \
                   f"Probabile stop quote nelle prossime ore.\n" \
                   f"Sport con dati: {n_sports}\n" \
                   f"\u23f0 Reset piano: 01/10/2026"
            await _send_report_to_recipients(context, text)
        elif remaining <= 10:
            text = f"\ud83d\udd34 **Crediti bassi**\n\n" \
                   f"Remaining: **{remaining}** crediti ({n_sports} sport)\n" \
                   f"Fascia di allarme attiva. Ridurre rotazione non-core.\n" \
                   f"\u23f0 Reset: 01/10/2026"
            await _send_report_to_recipients(context, text)
        elif remaining <= 20:
            text = f"\ud83d\udfe0 **Crediti sotto la soglia**\n\n" \
                   f"Remaining: **{remaining}** crediti ({n_sports} sport)\n" \
                   f"Sotto MIN_REMAINING ({remaining} < 20).\n" \
                   f"Attenzione: le prossime chiamate API potrebbero fallire."
            await _send_report_to_recipients(context, text)
        elif remaining <= 50:
            text = f"\ud83d\udfe1 **Crediti in calo**\n\n" \
                   f"Remaining: **{remaining}** crediti ({n_sports} sport)\n" \
                   f"Consumo ~6/giorno. Reset 01/10.\n" \
                   f"Monitorare: {remaining} / 6 = ~{remaining//6} giorni rimasti."
            await _send_report_to_recipients(context, text)

        # --- RITMO DI CONSUMO (15/09): la soglia assoluta tace sopra 50 e
        # --- avvisa quando il budget e' gia' compromesso. Il ritmo MISURATO
        # --- dice invece se i crediti arrivano al reset: se il consumo
        # --- proietta l'esaurimento prima, l'admin lo scopre oggi e puo'
        # --- ridurre rotazione/settlement (anti-spam: 1 alert/giorno).
        try:
            from odds_api import credit_budget_status
            from tracker import is_notified, mark_notified
            b = credit_budget_status()
            logger.info("credit_watchdog: residuo %s, ritmo %s/giorno "
                        "(finestra %sh, %s letture), esaurimento %s, "
                        "reset tra %s giorni, sostenibile %s/giorno",
                        b["remaining"], b["rate_per_day"], b["window_hours"],
                        b["samples"], b["exhaustion_date"],
                        b["days_to_reset"], b["sustainable_per_day"])
            if b.get("alert"):
                from datetime import timezone as _tz, timedelta as _td
                # Ora italiana (IT_OFFSET in `main()` e' locale: qui +2).
                day = (datetime.now(_tz.utc)
                       + _td(hours=2)).strftime("%Y-%m-%d")
                if not is_notified("CREDIT_BURN", day):
                    mark_notified("CREDIT_BURN", day)
                    text = (
                        "\u23f3 *CREDITI the-odds-api: il BUDGET NON ARRIVA "
                        "AL RESET*\n\n"
                        f"• Residuo: **{b['remaining']}** crediti\n"
                        f"• Consumo misurato: **{b['rate_per_day']}/giorno** "
                        f"(finestra {b['window_hours']:.0f}h, "
                        f"{b['samples']} letture)\n"
                        f"• Esaurimento previsto: **{b['exhaustion_date']}**\n"
                        f"• Reset del piano tra {b['days_to_reset']} giorni "
                        f"({b['sustainable_per_day']}/giorno sostenibili)\n\n"
                        "Da ridurre (in ordine di costo): refetch del "
                        "settlement per le leghe con righe aperte, rotazione "
                        "quote (`ODDS_DAILY_BUDGET`), surebet. Le soglie "
                        "proattive di `should_query_sport` scattano solo "
                        "sotto 50 crediti: ora sei ancora sopra.")
                    if context is not None:
                        await _send_report_to_recipients(context, text)
                    else:
                        logger.warning("credit_watchdog: %s",
                                       text.replace("\n", " | "))
        except Exception as e:
            logger.warning("credit_watchdog: ritmo non valutabile (%s)", e)
    except Exception as e:
        logger.error(f"credit_watchdog_job error: {e}")


async def liquidity_monitor_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Monitor scarti liquidita' SX Bet ogni 6h (11/09/2026).

    Legge il log degli scarti (data/execution/liquidity_skips.jsonl) e
    allerta admin+iscritti SOLO se nelle ultime 24h ci sono scarti. Lo
    stato viene SEMPRE loggato (telemetria continua); la notifica e' il
    campanello, con anti-spam 1 alert/giorno (chiave LIQ_SKIP).

    Perche' conta: su un exchange ogni scarto per book sottile e' un'edge
    potenzialmente persa — se gli scarti crescono, o le soglie SX_MIN_*
    sono troppo severe o i mercati sono troppo illiquidi per puntarci.
    """
    try:
        from liquidity_monitor import summary, format_report
        s = summary(days=1)
        logger.info("liquidity_monitor: %d scarti in 24h (edge perso %.2f, "
                    "stake non investito %.2f)", s["events"],
                    s["missed_profit"], s["stake_skipped"])
        if not s["events"]:
            return
        from tracker import is_notified, mark_notified
        from datetime import timezone as _tz, timedelta as _td
        day = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
        text = format_report(days=1)
        if not text or is_notified("LIQ_SKIP", day):
            return
        mark_notified("LIQ_SKIP", day)
        if context is not None:
            await _send_report_to_recipients(context, text)
        else:
            logger.warning("liquidity_monitor: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("liquidity_monitor_job fallito: %s", e)


async def order_watch_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Verifica gli ORDINI REALI ogni 30' (28/09/2026).

    Dal 28/09 valgono due invarianti sul DENARO: stake fisso 1.50 USDC per
    ordine e recinto del 40% sull'esposizione APERTA. Un ordine che li viola
    puo' arrivare in qualsiasi momento e senza un controllo che si ripete
    nessuno se ne accorgerebbe fino al drawdown.

    Sola lettura del ledger (zero crediti, zero ordini). Lo stato viene SEMPRE
    loggato; la notifica parte SOLO sulle violazioni, con anti-spam 1/giorno
    (chiave ORDER_WATCH): un allarme ripetuto ogni 30' viene ignorato, uno che
    arriva una volta viene letto.
    """
    try:
        import order_watch
        data = order_watch.audit()
        o = data.get("orders") or {}
        logger.info("order_watch: %d ordini live (%d dopo la direttiva), "
                    "%d aperti, %.2f USDC esposti — %s", o.get("live", 0),
                    o.get("live_dopo_direttiva", 0), o.get("aperte", 0),
                    data.get("esposizione_corrente", 0.0), data.get("verdict"))
        if not (data.get("violations") or []):
            return
        from tracker import is_notified, mark_notified
        from datetime import timezone as _tz, timedelta as _td
        day = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
        if is_notified("ORDER_WATCH", day):
            return
        mark_notified("ORDER_WATCH", day)
        text = "🚨 " + order_watch.format_report(data)
        if context is not None:
            await _send_report_to_recipients(context, text)
        else:
            logger.warning("order_watch: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("order_watch_job fallito: %s", e)


async def book_flow_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Flusso dell'order book SX ogni 6h (26/09/2026).

    Legge il registro degli INGRESSI di liquidita' (`book_flow.py`:
    data/execution/book_flow_events.jsonl) e allerta admin+iscritti SOLO se
    nelle ultime 24h il book si e' riempito in modo anomalo su qualche esito.
    Lo stato viene SEMPRE loggato (telemetria continua); la notifica e' il
    campanello, con anti-spam 1 alert/giorno (chiave BOOK_FLOW).

    ⚠️ E' TELEMETRIA: il modulo non piazza ordini e non tocca i gate di
    strategia. Zero costi API: legge solo il JSONL sul volume.
    """
    try:
        from book_flow import summary, format_report
        s = summary(days=1)
        logger.info("book_flow: %d ingressi di liquidita' in 24h "
                    "(+%.0f USDC, firme %s)", s["events"],
                    s["total_delta_usdc"], s["by_reason"])
        if not s["events"]:
            return
        from tracker import is_notified, mark_notified
        from datetime import timezone as _tz, timedelta as _td
        day = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
        text = format_report(days=1)
        if not text or is_notified("BOOK_FLOW", day):
            return
        mark_notified("BOOK_FLOW", day)
        if context is not None:
            await _send_report_to_recipients(context, text)
        else:
            logger.warning("book_flow: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("book_flow_job fallito: %s", e)


async def hedge_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Smart hedging pre-match, ogni 15' (26/09/2026).

    Valuta le posizioni LIVE aperte (finestra HEDGE_MIN_MINUTES..HORIZON_h
    dal kickoff) e copre quelle la cui quota si e' ACCORCIATA di almeno
    HEDGE_MIN_MOVE_PCT: due gambe complementari (hedge_plan) bloccano il
    profitto qualunque sia l'esito. L'esecuzione DELEGA a `auto_bet._live_fill`
    (stessi guardrail: floor EV, liquidita', T-60, blocchi) e le gambe sono
    registrate sul ledger `bets` (mode='live', mercato='1X2') cosi' il
    settlement esistente le salda da solo.

    Notifica: UN messaggio per hedge PIAZZATO (e' un ordine reale, come le
    notifiche FULLY_FILLED — niente anti-spam sugli ordini); gli scarti
    restano su log + JSONL (telemetria, non rumore in chat). Gated da
    SMART_HEDGING=0 per spegnerlo.
    """
    try:
        from smart_hedging import run_hedge_cycle, format_alert
        res = run_hedge_cycle()
        logger.info("hedge: valutate %d, opportunita' %d, piazzate %d, "
                    "scartate %d%s", res["evaluated"], res["opportunities"],
                    len(res["placed"]), len(res["skipped"]),
                    f", bloccato ({res['blocked']})" if res.get("blocked")
                    else "")
        for entry in res["placed"]:
            text = format_alert(entry)
            if context is not None:
                await _send_report_to_recipients(context, text)
            else:
                logger.warning("hedge alert: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("hedge_job fallito: %s", e)


async def decision_compare_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Confronto shadow catena ↔ corsia, ogni 6h (16/09/2026).

    Dal 15/09 la catena `decision/` valuta in shadow mode gli stessi segnali
    che la corsia sta per giocare, e dal 16/09 ne persiste il verdetto sul
    ledger `decisions`. Qui le due strade vengono messe a confronto su
    (match, esito): quante volte concordano, **quante puntate REALI la catena
    avrebbe rifiutato** (e con che P/L) e quante opportunita' la corsia ha
    saltato mentre la catena le avrebbe giocate.

    Zero costi: legge solo i due ledger locali (`decisions`, `bets`) in sola
    lettura — nessun ordine, nessun credito the-odds-api. Lo stato viene
    SEMPRE loggato (e' la serie storica della fase di misura); la notifica va
    SOLO agli admin (il confronto e' materiale di ingegneria, non un segnale
    per gli iscritti) e solo se c'e' almeno una divergenza, con anti-spam 1
    alert/giorno (chiave `SHADOW_COMPARE`).
    """
    try:
        from decision.compare import (compare_enabled, format_report, measure,
                                      window_days)
        if not compare_enabled():
            return
        days = window_days()
        data = measure(days=days)
        if data.get("error"):
            logger.warning("decision_compare: misura non disponibile (%s)",
                           data["error"])
            return
        chain = data.get("chain") or {}
        lane = data.get("lane") or {}
        agr = data.get("agreement") or {}
        logger.info(
            "decision_compare: catena %d righe (avrebbe giocato %d) | corsia %d "
            "puntate | entrambe giocano %d | bloccate-ma-giocate %d | "
            "giocate-ma-saltate %d | non confrontabili %d | divergenza %s",
            chain.get("rows", 0), chain.get("would_play", 0), lane.get("bets", 0),
            agr.get("both_play", 0), agr.get("blocked_played", 0),
            agr.get("would_play_skipped", 0), agr.get("unobserved", 0),
            agr.get("divergence_rate"))
        if not agr.get("divergences"):
            return              # nessuna divergenza: il log basta, niente messaggio
        from datetime import timezone as _tz, timedelta as _td
        day = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
        if is_notified("SHADOW_COMPARE", day):
            return
        mark_notified("SHADOW_COMPARE", day)
        text = ("🔀 *CONFRONTO SHADOW* — catena vs corsia\n\n"
                + format_report(data))
        if context is not None:
            # Solo admin: e' una misura interna (passo 3), non un segnale.
            for chat_id in _admin_chat_ids():
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text,
                                                   parse_mode="Markdown")
                except Exception:
                    pass
        else:
            logger.warning("decision_compare: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("decision_compare_job fallito: %s", e)


async def end_of_day_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo quando FINISCE L'ULTIMA PARTITA della giornata.

    Controlla ogni 15 minuti (dalle 21:00): quando tutte le partite del
    giorno iniziate hanno il risultato, invia il riepilogo una volta sola.
    Fallback notturno (23:50 UTC): se qualche partita non si chiude
    (rinvio, dati lenti), invia comunque per non perdere la giornata.
    """
    from tracker import day_completed, is_notified, mark_notified
    from datetime import timezone as _tz, timedelta as _td
    # Ora italiana (UTC+2 estive)
    now_it = datetime.now(_tz.utc) + _td(hours=2)
    today = now_it.strftime("%Y-%m-%d")
    if is_notified("EOD", today):
        return
    forced = now_it.hour >= 23 and now_it.minute >= 50
    if not forced and not day_completed(today):
        return
    text = format_daily_report(today, "OGGI — FINE GIORNATA")
    await _send_report_to_recipients(context, text)
    mark_notified("EOD", today)
    logger.info("Riepilogo di fine giornata inviato (ultima partita chiusa).")


async def t60_job(context: ContextTypes.DEFAULT_TYPE):
    """Job strategia T-60 (ogni minuto): dispatch esecutivo nella finestra T-60.

    La SCANSIONE del palinsesto e la valutazione dei segnali restano nei giri
    normali (auto_bet ogni 60s + analisi): questo job e' la DECISIONE
    ESECUTIVA — un'ora prima del fischio d'inizio esegue gli ordini sulle
    righe validate, con i 4 circuit breakers attivi:
    CB1 cap 1 USDC/ordine (Kelly ignorato) · CB2 kill switch wallet 30 USDC
    (arresto + alert) · CB3 contratto Pydantic rigido (malformato -> riga di
    rifiuto sul ledger) · CB4 gate di mercato/feed SX fail-closed.
    Fail-safe totale: un errore non tocca mai il giro auto_bet normale.
    """
    try:
        from auto_bet import t60_kill_switch_status, t60_dispatch_pending
    except ImportError as e:
        logger.warning("t60_job: auto_bet T60 non disponibile: %s", e)
        return
    # CB2: wallet a/ sotto 30 USDC (o flag armato) = arresto. Il flag e'
    # scritto dal giro (auto_bet/t60 dispatch) leggendo il wallet REALE; qui
    # si rispetta il blocco e si lascia il promemoria al watch job.
    if t60_kill_switch_status().get("triggered"):
        return
    loop = asyncio.get_running_loop()
    try:
        placed = await loop.run_in_executor(_scan_executor,
                                            t60_dispatch_pending)
    except Exception as e:
        logger.error("t60_job: %s", e)
        return
    for p in placed:
        if p.get("mode") == "t60-live":
            try:
                text = ("🎯 *ORDINE T-60 ESEGUITO (LIVE)*\n\n"
                        f"• {p.get('home')} vs {p.get('away')} — "
                        f"{p.get('esito_key')} @ {p.get('price', 0):.2f}\n"
                        f"💰 Stake: {p.get('stake', 0):.2f} USDC "
                        f"(CB1 cap 1.00)\n"
                        f"🆔 Bet: `{p.get('bet_id')}`\n"
                        f"📍 Kickoff: T-60 minuti")
                await _send_report_to_recipients(context, text)
            except Exception as e:
                logger.warning("t60_job notifica: %s", e)


async def t60_kill_watch_job(context: ContextTypes.DEFAULT_TYPE):
    """CB2 (ogni 6h): verifica il kill switch patrimoniale T-60.

    Logga SEMPRE lo stato; se il flag e' armato invia l'alert di emergenza a
    admin+iscritti con anti-spam 1/giorno (chiave T60_KILL). Zero costi:
    legge il flag sul volume, nessuna API.
    """
    try:
        from auto_bet import t60_kill_switch_status
        st = t60_kill_switch_status()
        if not st.get("triggered"):
            logger.info("t60_kill_watch: CB2 ok (wallet sopra la soglia "
                        "%.2f USDC, nessun flag)", st.get("threshold", 30.0))
            return
        logger.error("t60_kill_watch: KILL SWITCH T-60 ATTIVO — %s",
                     st.get("reason"))
        from tracker import is_notified, mark_notified
        from datetime import timezone as _tz
        today = datetime.now(_tz.utc).strftime("%Y-%m-%d")
        if is_notified("T60_KILL", today):
            return
        text = ("🚨 *KILL SWITCH T-60 ATTIVO*\n\n"
                f"{st.get('reason') or 'wallet sotto la soglia'}\n\n"
                "⛔ Processi di puntata ARRESTATI (CB2).\n"
                "Riattivazione: top-up wallet poi `/t60reset`.")
        await _send_report_to_recipients(context, text)
        mark_notified("T60_KILL", today)
    except Exception as e:
        logger.error("t60_kill_watch_job: %s", e)


async def cmd_t60reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/t60reset (solo admin): disinnesca il CB2 (kill switch patrimoniale).

    Non riattiva nulla da solo: al prossimo giro il wallet viene RILETTO —
    se l'equity e' tornata sopra la soglia le puntate ripartono, altrimenti
    il flag si riarma da solo. E' la protezione contro il riarmo immediato
    dopo un top-up dimenticato.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    from auto_bet import (_live_wallet_snapshot, t60_check_wallet_kill,
                          t60_clear_kill, t60_kill_switch_status)
    was = t60_kill_switch_status()
    t60_clear_kill()
    snap = _live_wallet_snapshot()
    equity = snap["equity"] if snap else None
    rearmed = t60_check_wallet_kill(equity)
    if rearmed:
        await update.message.reply_text(
            "🚨 *CB2 RIARMATO IMMEDIATAMENTE*\n\n"
            f"Equity wallet: {equity if equity is not None else 'non leggibile'}\n"
            "Ancora a/ sotto la soglia: il kill switch resta attivo.",
            parse_mode="Markdown")
        return
    prev = (was.get("reason") or "nessun flag precedente") \
        if was.get("triggered") else "nessun flag precedente"
    await update.message.reply_text(
        "✅ *CB2 DISINNESSO*\n\n"
        f"Flag precedente: {prev}\n"
        f"Equity wallet attuale: {equity if equity is not None else 'non leggibile'}\n"
        "Le puntate ripartono col prossimo giro (il wallet viene riletto "
        "a ogni giro).", parse_mode="Markdown")


async def cmd_stopreset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stopreset [stato] (solo admin): azzera lo stop-loss GIORNALIERO.

    Direttiva 03/10/2026: `auto_bet.clear_daily_stop()` esisteva per la
    rimozione manuale, ma nessun comando Telegram la esponeva — con il blocco
    attivo le puntate restavano ferme 24h e l'unica alternativa era aspettare
    la scadenza.

    Il comando NON indebolisce il guardrail: azzera il blocco in corso e
    dichiara (a) che il riferimento del giorno verra' RICREATO dal giro
    successivo sull'equity ATTUALE (una perdita >= soglia lo riarma subito) e
    (b) lo stato degli ALTRI guardrail che precedono l'ordine (stop
    settimanale e CB2): "sbloccato" non deve essere una promessa vuota.

    `clear_weekly_stop()` NON viene chiamato da qui: il blocco settimanale e'
    un'autorita' distinta e si azzera solo con una scelta esplicita.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    from auto_bet import (DAILY_STOP_LOSS_PCT, _live_wallet_snapshot,
                          clear_daily_stop, daily_stop_status,
                          t60_kill_switch_status, weekly_stop_status)
    arg = (context.args[0] if context.args else "").strip().lower()
    only_status = arg in ("stato", "status")
    before = daily_stop_status()
    was_stopped = bool(before.get("stopped"))
    # Si azzera SOLO un blocco davvero attivo: cancellare un file che contiene
    # il solo riferimento del giorno (blocco scaduto o mai scattato) ne
    # azzererebbe la contabilita' — la perdita accumulata oggi sparirebbe dal
    # confronto e il -5% verrebbe misurato da capo.
    if not only_status and was_stopped:
        clear_daily_stop()

    # Stato reale degli altri guardrail nella catena di run_today_bets:
    # daily -> weekly -> CB2. Senza dichiararli, "azzerato" sembrerebbe una
    # ripartenza mentre il giro si ferma al breaker successivo.
    _ws = weekly_stop_status()
    _cb2 = t60_kill_switch_status()
    try:
        snap = _live_wallet_snapshot()
    except Exception:
        snap = None
    equity = snap["equity"] if snap else None

    weekly_txt = (f"🛑 *ATTIVO* fino a {str(_ws.get('until'))[:16]} "
                  f"(drawdown -{(_ws.get('drawdown_pct') or 0):.1f}%, "
                  f"soglia -{_ws['loss_pct']:.0f}%)"
                  if _ws.get("stopped") else
                  f"🟢 non attivo (soglia -{_ws['loss_pct']:.0f}%)")
    cb2_txt = (f"🛑 *ATTIVO* ({_cb2.get('reason') or 'soglia wallet'})"
               if _cb2.get("triggered") else
               f"🟢 non attivo (soglia {float(_cb2.get('threshold') or 0):.2f} "
               f"USDC di equity)")

    lines: list = []
    if only_status:
        head = (("🛑 *STOP-LOSS GIORNALIERO ATTIVO*\n\n"
                 f"• Fino a: `{str(before.get('until'))[:16]}`\n"
                 f"• Motivo: {before.get('reason') or 'perdita giornaliera'}\n"
                 f"• Riferimento del giorno: {before.get('start_bankroll')} "
                 f"USDC (base `{before.get('basis_key')}`)\n\n")
                if was_stopped else
                "🟢 *STOP-LOSS GIORNALIERO non attivo*\n\n")
        lines.append("Per azzerarlo: `/stopreset`")
    elif was_stopped:
        head = ("✅ *STOP-LOSS GIORNALIERO AZZERATO*\n\n"
                f"Prima: 🛑 attivo fino a `{str(before.get('until'))[:16]}`\n"
                f"• Motivo: {before.get('reason') or 'perdita giornaliera'}\n"
                f"• Riferimento del giorno: {before.get('start_bankroll')} "
                f"USDC (base `{before.get('basis_key')}`)\n\n")
        lines.append(
            "Ora: 🟢 non attivo → il giro di puntate (ogni 60s) riparte.")
        if equity is not None:
            lines.append(
                f"⚠️ Il giro successivo RICREA il riferimento del giorno "
                f"sull'equity ATTUALE ({equity:.2f} USDC): una perdita ≥ "
                f"{equity * DAILY_STOP_LOSS_PCT:.2f} USDC "
                f"({DAILY_STOP_LOSS_PCT * 100:.0f}%) lo riarma per 24h.")
    else:
        head = ("ℹ️ *STOP-LOSS GIORNALIERO non era attivo*\n\n"
                "Nessun blocco da azzerare: il giro di puntate era già libero.\n\n")
        lines.append("Nessuna modifica applicata al riferimento del giorno.")

    lines.append("")
    lines.append("*Altri guardrail PRIMA dell'ordine*")
    lines.append(f"• Settimanale (7g): {weekly_txt}")
    lines.append(f"• CB2 patrimoniale: {cb2_txt}")
    if _ws.get("stopped") or _cb2.get("triggered"):
        blocker = []
        if _ws.get("stopped"):
            blocker.append("stop settimanale")
        if _cb2.get("triggered"):
            blocker.append("CB2 (`/t60reset`)")
        lines.append("")
        lines.append("⚠️ *Il giro restera' comunque fermo per: "
                     + ", ".join(blocker) + ".*")
    await update.message.reply_text(head + "\n".join(lines),
                                    parse_mode="Markdown")


async def cmd_weeklyreset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/weeklyreset [stato] (solo admin): azzera il circuit breaker SETTIMANALE.

    Direttiva 03/10/2026: `auto_bet.clear_weekly_stop()` esisteva per la
    rimozione manuale ma era chiamata solo da test e `verify_guardrails.py` —
    con il blocco attivo le puntate restavano ferme 24h e l'unica alternativa
    era aspettare la scadenza.

    Stessa logica di sicurezza di `/stopreset`: solo admin, `stato` per la
    sola lettura, si azzera SOLO un blocco davvero attivo, e si dichiara lo
    stato REALE degli altri guardrail (daily, CB2) — "azzerato" non deve
    sembrare una ripartenza se un'autorita' successiva tiene il giro fermo.

    ⚠️ DIFFERENZA IMPORTANTE rispetto al daily, ed e' il motivo per cui questo
    comando e' nato: azzerare il FILE non basta. Se il drawdown rolling e'
    ANCORA sopra soglia, il giro successivo (60 s) lo riarma, perche' lo
    storico del drawdown e' intatto e il picco si ricalcola identico. Qui la
    previsione si CALCOLA e si DICHIARA, invece di promettere una ripartenza
    che non avverra'.
    """
    admin_ids = _admin_chat_ids()
    if admin_ids and update.effective_chat.id not in admin_ids:
        await update.message.reply_text("⛔ Comando riservato agli admin.")
        return
    from auto_bet import (WEEKLY_STOP_LOSS_PCT, _live_wallet_snapshot,
                          clear_weekly_stop, daily_stop_status,
                          t60_kill_switch_status, weekly_drawdown,
                          weekly_stop_status)
    arg = (context.args[0] if context.args else "").strip().lower()
    only_status = arg in ("stato", "status")
    before = weekly_stop_status()
    was_stopped = bool(before.get("stopped"))
    # Si azzera SOLO un blocco davvero attivo (come `/stopreset`).
    if not only_status and was_stopped:
        clear_weekly_stop()

    try:
        snap = _live_wallet_snapshot()
    except Exception:
        snap = None
    equity = snap["equity"] if snap else None

    # Previsione di RIARMO: con lo storico INTATTO il picco rolling ricalcola
    # la stessa perdita, quindi il blocco torna al giro successivo.
    dd = None
    if equity is not None:
        try:
            dd = weekly_drawdown(equity)
        except Exception:
            dd = None

    lines: list = []
    if only_status:
        head = (("🛑 *STOP-LOSS SETTIMANALE ATTIVO*\n\n"
                 f"• Fino a: `{str(before.get('until'))[:16]}`\n"
                 f"• Motivo: {before.get('reason') or 'drawdown rolling'}\n"
                 f"• Picco rolling: {before.get('peak')} USDC "
                 f"(soglia -{before['loss_pct']:.0f}%, finestra "
                 f"{before['window_h']:.0f}h)\n\n")
                if was_stopped else
                "🟢 *STOP-LOSS SETTIMANALE non attivo*\n\n")
        lines.append("Per azzerarlo: `/weeklyreset`")
    elif was_stopped:
        head = ("✅ *STOP-LOSS SETTIMANALE AZZERATO*\n\n"
                f"Prima: 🛑 attivo fino a `{str(before.get('until'))[:16]}`\n"
                f"• Motivo: {before.get('reason') or 'drawdown rolling'}\n"
                f"• Picco rolling: {before.get('peak')} USDC\n\n")
    else:
        head = ("ℹ️ *STOP-LOSS SETTIMANALE non era attivo*\n\n"
                "Nessun blocco da azzerare: nessuna modifica applicata.\n\n")

    if dd is not None and (dd.get("drawdown_pct") or 0) / 100.0 >= \
            WEEKLY_STOP_LOSS_PCT:
        peak = float(dd.get("peak") or 0.0)
        need = peak * (1.0 - WEEKLY_STOP_LOSS_PCT)
        lines.append(
            f"⚠️ *Il blocco si RIARMA entro 60 secondi.* Il drawdown rolling "
            f"e' ancora -{float(dd['drawdown_pct']):.1f}% (soglia "
            f"-{WEEKLY_STOP_LOSS_PCT * 100:.0f}%, picco {peak:.2f} USDC): lo "
            f"storico del drawdown e' INTATTO, quindi il giro successivo "
            f"ricalcola la stessa perdita.")
        lines.append(
            f"Per ripartire davvero: equity ≥ *{need:.2f} USDC*, oppure "
            f"azzerare anche la BASE del drawdown "
            f"(`data/execution/bankroll_history.json`) — che pero' disarma il "
            f"breaker finche' lo storico non si ricostruisce.")
    elif was_stopped:
        lines.append("Ora: 🟢 non attivo → il giro di puntate (ogni 60s) "
                     "riparte." + ("" if dd is not None else
                                    " (drawdown non ricalcolabile: "
                                    "verificare al prossimo giro)."))

    # Stato reale delle altre autorita' che precedono l'ordine: senza
    # dichiararle, "azzerato" sembrerebbe una ripartenza a vuoto.
    _ds = daily_stop_status()
    _cb2 = t60_kill_switch_status()
    daily_txt = (f"🛑 *ATTIVO* fino a {str(_ds.get('until'))[:16]}"
                 if _ds.get("stopped") else "🟢 non attivo")
    cb2_txt = (f"🛑 *ATTIVO* ({_cb2.get('reason') or 'soglia wallet'})"
               if _cb2.get("triggered") else
               f"🟢 non attivo (soglia "
               f"{float(_cb2.get('threshold') or 0):.2f} USDC di equity)")
    if equity is not None:
        lines.append(f"Equity wallet attuale: {equity:.2f} USDC")
    lines.append("")
    lines.append("*Altri guardrail PRIMA dell'ordine*")
    lines.append(f"• Giornaliero: {daily_txt}")
    lines.append(f"• CB2 patrimoniale: {cb2_txt}")
    blockers = []
    if _ds.get("stopped"):
        blockers.append("stop giornaliero (`/stopreset`)")
    if _cb2.get("triggered"):
        blockers.append("CB2 (`/t60reset`)")
    if blockers:
        lines.append("")
        lines.append("⚠️ *Il giro restera' comunque fermo per: "
                     + ", ".join(blockers) + ".*")
    await update.message.reply_text(head + "\n".join(lines),
                                    parse_mode="Markdown")


async def report_morning_job(context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo del mattino (08:05 ITA): cosa è successo ieri.

    Direttiva 02/10/2026: l'invio Telegram del "Riepilogo di Ieri" e'
    DISATTIVATO di default. Il report resta calcolato e leggibile nei log
    (telemetria intatta) e si riattiva con l'env `MORNING_REPORT_NOTIFY=1`.
    Il comando manuale `/riepilogo` non e' toccato (l'utente lo invoca a
    richiesta).
    """
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    text = format_daily_report(yesterday, "IERI")
    if not _notify_enabled("MORNING_REPORT_NOTIFY"):
        logger.info("Riepilogo di ieri: invio Telegram disattivato "
                    "(MORNING_REPORT_NOTIFY=1 per riattivarlo) — %s",
                    text.replace("\n", " | ")[:500])
        return
    await _send_report_to_recipients(context, text)
    logger.info("Riepilogo di ieri inviato agli iscritti.")


async def auto_bet_job(context: ContextTypes.DEFAULT_TYPE):
    """Giro puntate automatiche (ogni minuto, 24/7 dal 09/09).

    SIM di default (paper trading con la quota del segnale, registrate in
    `bets` e saldate a fine partita) oppure LIVE se AUTO_BET_MODE=live e
    provider reale configurato (ordini SX reali con staking dinamico sul
    saldo del wallet). Gira ogni minuto: i nuovi segnali value delle
    analisi (04:00/12:00/18:00 UTC) vengono scommessi entro 1 minuto e il
    floor EV cattura i miglioramenti di prezzo fino alla guardia dei 15
    min pre-kickoff; UNIQUE(match_id, esito) evita doppioni e il cap
    esposizione e' giornaliero (sottrae l'esposizione dei giri precedenti).
    """
    loop = asyncio.get_running_loop()
    try:
        placed = await loop.run_in_executor(_scan_executor, run_today_bets,
                                            None, True)
    except Exception as e:
        logger.error("auto_bet_job: %s", e)
        return
    if not placed:
        # Nessuna puntata piazzata: se e' colpa del kill-switch OFF o dello
        # stop-loss giornaliero avvisa (in emergenza la visibilita' e'
        # tutto); altrimenti silenzio. Anti-spam: max 1 alert/giorno per
        # causa (il giro gira ogni minuto).
        try:
            from auto_bet import (kill_switch_status, daily_stop_status,
                                   weekly_stop_status)
            from tracker import is_notified, mark_notified
            from datetime import timezone as _tz, timedelta as _td
            today = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
            if kill_switch_status().get("effective") == "off":
                if not is_notified("KS_OFF", today):
                    text = ("🛑 *AUTO-BET BLOCCATO (kill-switch OFF)*\n\n"
                            "Il giro delle puntate automatiche e' stato "
                            "saltato: nessuna puntata piazzata.\n"
                            "Per riattivare: `/autobet live`")
                    await _send_report_to_recipients(context, text)
                    mark_notified("KS_OFF", today)
                return
            _ds = daily_stop_status()
            if _ds.get("stopped"):
                if not is_notified("DAILY_STOP", today):
                    text = ("🛑 *STOP-LOSS GIORNALIERO ATTIVO*\n\n"
                            f"Bankroll sotto del {_ds['loss_pct']:.0f}% "
                            f"dall'inizio giornata: {_ds.get('reason') or ''}\n"
                            f"Puntate bloccate fino a {_ds.get('until')}.\n"
                            "Si riattiva da solo dopo le 24h; per farlo "
                            "prima rimuovi `data/execution/daily_stop.json`.")
                    await _send_report_to_recipients(context, text)
                    mark_notified("DAILY_STOP", today)
                return
            _ws = weekly_stop_status()
            if _ws.get("stopped"):
                if not is_notified("WEEKLY_STOP", today):
                    text = ("📉 *CIRCUIT BREAKER SETTIMANALE ATTIVO*\n\n"
                            f"Drawdown rolling {_ws['window_h']:.0f}h: "
                            f"-{(_ws.get('drawdown_pct') or 0):.1f}% "
                            f"(soglia -{_ws['loss_pct']:.0f}%)\n"
                            f"{_ws.get('reason') or ''}\n"
                            f"Puntate bloccate fino a {_ws.get('until')}.\n"
                            "Si riarma da solo quando il picco esce dalla "
                            "finestra; per farlo prima rimuovi "
                            "`data/execution/weekly_stop.json`.")
                    await _send_report_to_recipients(context, text)
                    mark_notified("WEEKLY_STOP", today)
                return
            # Gate di mercato: il giro non parte senza un feed fresco,
            # conforme e VALIDATO (sorgente primaria SX Bet). Lo stop resta
            # finche' la validazione non e' raggiunta: e' la condizione
            # richiesta prima di riaprire le puntate automatiche.
            from auto_bet import market_gate_status
            _mg = market_gate_status()
            if _mg.get("blocked"):
                if not is_notified("FEED_BLOCKED", today):
                    text = ("📡 *PUNTATE FERME: GATEWAY DI MERCATO*\n\n"
                            f"Motivo: `{_mg.get('reason')}` — {_mg.get('detail')}\n"
                            f"Gateway: {_mg.get('gateway_id') or '-'} "
                            f"| sorgente {_mg.get('source') or '-'}\n"
                            f"Feed validato: {'sì' if _mg.get('validated') else 'no'}"
                            f" | quote lette: {_mg.get('accepted')}\n\n"
                            "Le puntate automatiche restano fermi finché il feed "
                            "non è fresco, conforme al contratto e validato.\n"
                            "Verifica: `/autobet` oppure "
                            "`python -m decision feed --refresh --force`")
                    await _send_report_to_recipients(context, text)
                    mark_notified("FEED_BLOCKED", today)
                    return
        except Exception as e:
            logger.error("auto_bet_job (blocked notify): %s", e)
        return
    mode = placed[0]["mode"]
    mode_label = {"live": "LIVE", "sim": "SIMULAZIONE",
                  "dry-run": "DRY-RUN"}.get(mode, mode)
    total = sum(p["stake"] for p in placed)
    _lane = {"chief": " 🏛️catena"}
    rows = "\n".join(
        f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
        f"(€{p['stake']:.2f}){_lane.get(p.get('lane'), '')}" for p in placed)
    text = (f"🎯 *PUNTATE AUTOMATICHE ({mode_label})*\n"
            f"{len(placed)} puntate, €{total:.2f} di stake\n\n{rows}\n\n"
            f"📌 {'ORDINI REALI' if mode == 'live' else 'Simulazione: nessun ordine reale inviato.'}")
    await _send_report_to_recipients(context, text)
    logger.info("auto_bet_job: %d puntate (%s), €%.2f", len(placed), mode, total)

    # Notifica real-time per ordini FULLY_FILLED
    try:
        filled = [p for p in placed if p.get("status") == "FULLY_FILLED"
                  and p.get("mode") == "live"]
        for p in filled:
            # Orario di inizio in ora ITALIANA (direttiva 02/10/2026). Se il
            # timestamp manca o non e' parsabile `format_match_start` ritorna
            # None: si omette la riga, mai un orario inventato.
            _start = format_match_start(p.get("commence"))
            _start_line = f"{_start}\n" if _start else ""
            msg = (f"✅ *ORDINE FULLY_FILLED*"
                   f"{' 🏛️catena Chief' if p.get('lane') == 'chief' else ''}"
                   f"\n\n"
                   f"🏟️ {p['home']} vs {p['away']}\n"
                   f"{_start_line}"
                   f"🎯 {p['esito_key']} @ {p['price']:.2f}\n"
                   f"💰 Stake: €{p['stake']:.2f}\n"
                   f"📋 Bet ID: `{p.get('bet_id', 'N/A')}`\n"
                   f"📈 ROI atteso: {(p.get('price', 0) / p.get('prob', 1) - 1) * 100:.1f}%")
            await _send_report_to_recipients(context, msg)
    except Exception as e:
        logger.warning("auto_bet_job (fully_filled notify): %s", e)


async def sx_signals_job(context: ContextTypes.DEFAULT_TYPE):
    """Scan SX Bet (ogni SX_SCAN_INTERVAL_MIN, default 15') + settlement sx-*.

    Genera segnali value 1X2 SOLO dai prezzi dell'order book SX Bet (API
    pubblica: zero chiavi, zero crediti the-odds-api) e li salva nel ledger
    predictions/matches/match_analysis: il giro auto-bet (ogni minuto) li
    vede come qualunque altro segnale value e — con AUTO_BET_MODE=live e
    provider SX configurato — piazza l'ordine reale sullo STESSO exchange
    che ha generato il prezzo (resolve_match_market matcha nomi+kickoff,
    e il floor EV protegge dai movimenti avversi). Fa anche da settlement
    dedicato per le bet sx-* (punteggi via the-odds-api se configurata,
    altrimenti API-Football): senza risultato disponibile le bet restano
    aperte (fail-closed). Silenzioso se non ci sono nuovi segnali.
    """
    if os.getenv("SX_SIGNALS_ENABLED", "1") != "1":
        return
    loop = asyncio.get_running_loop()

    def _pass():
        from sx_signals import scan, settle_sx_bets
        sig = scan()
        st = settle_sx_bets()
        return sig, st

    try:
        signals, settle_res = await loop.run_in_executor(_scan_executor, _pass)
    except Exception as e:
        logger.error("sx_signals_job: %s", e)
        return
    if settle_res.get("settled"):
        logger.info("sx_signals_job: %d bet SX saldate (fonte %s)",
                    settle_res["settled"], settle_res.get("source"))
    if not signals:
        return
    rows = "\n".join(
        f"  • {s['home']} vs {s['away']} — {s['esito']} @ {s['quota']:.2f} "
        f"(EV {s['ev'] * 100:+.1f}%) [{s['status']}]" for s in signals[:8])
    text = (f"📡 *SEGNALI SX BET* — {len(signals)} nuovi value salvati\n\n"
            f"{rows}\n\n"
            "📌 Prezzi dall'order book SX Bet (API pubblica): il giro "
            "auto-bet li piazzera' se LIVE e' attivo.")
    logger.info("sx_signals_job: %d nuovi segnali value", len(signals))


async def multi_market_job(context: ContextTypes.DEFAULT_TYPE):
    """Corsia multi-mercato OU/AH (19/09): ingest SX + Poisson + ledger.

    Legge i mercati a LINEA (type 2 = Over/Under, type 3 = Asian Handicap)
    dall'API PUBBLICA di SX Bet (zero chiavi, zero crediti the-odds-api),
    valida le quote col contratto 2.0 e le salva in `market_quotes`, calcola
    i candidati con Poisson (push-aware) e li registra in `predictions`.

    Da li' il giro auto-bet (ogni minuto) legge i pick: **AH con ordini
    reali** (ENABLE_LIVE_AH=1) e **OU con ordini reali solo quando e' PRONTO**
    — l'interruttore `ENABLE_LIVE_OU` autorizza, il gate `ou_readiness`
    abilita (>= OU_LIVE_MIN_CLOSURES chiusure dell'era nuova e ROI positivo,
    direttiva del 26/09/2026). Finche' non e' pronto i segnali OU si generano,
    si registrano e si misurano, ma non diventano ordini.
    Silenzioso se non ci sono segnali giocabili. MM_ENABLED=0 per spegnerlo.
    """
    if os.getenv("MM_ENABLED", "1") != "1":
        return
    loop = asyncio.get_running_loop()

    def _pass():
        import multi_market
        return multi_market.scan()

    try:
        found = await loop.run_in_executor(_scan_executor, _pass)
    except Exception as e:
        logger.error("multi_market_job: %s", e)
        return
    if not found:
        return
    live_n = sum(1 for f in found if f.get("live"))
    logger.info("multi_market_job: %d segnali giocabili (%d corsie live)",
                len(found), live_n)


async def market_shadow_job(context: ContextTypes.DEFAULT_TYPE):
    """Telemetria OMbra sui mercati SX NON calcistici (01/10/2026).

    Registra i mercati Basketball/American Football (tipi SX 28/342/226,
    famiglia "including overtime") **esclusivamente** su `market_quotes`: il
    modello Poisson e' calcistico e questi sport non si analizzano, si
    raccolgono per poterli misurare piu' avanti.

    Vincolo tassativo del proprietario: NESSUNA riga su `predictions` (una
    previsione a stake 0 non e' un esperimento, e' un dato falso che inquina
    ROI e calibrazione) — il vincolo e' strutturale, non una promessa:
    `market_shadow` non importa il salvataggio delle previsioni e un tripwire
    lo verifica sul sorgente.

    Costo: zero crediti, zero chiavi, zero ordini (solo API pubblica SX).
    Spento di default: `SHADOW_MARKET_ENABLED=1` per attivarlo.
    """
    import market_shadow
    if not market_shadow.enabled():
        return
    loop = asyncio.get_running_loop()

    def _pass():
        return market_shadow.run()

    try:
        summary = await loop.run_in_executor(_scan_executor, _pass)
    except Exception as e:                       # pragma: no cover
        logger.error("market_shadow_job: %s", e)
        return
    logger.info("market_shadow_job: %s mercati letti, %s quote su "
                "market_quotes (predictions intatte)",
                (summary or {}).get("records"), (summary or {}).get("saved"))


async def tennis_lane_job(context: ContextTypes.DEFAULT_TYPE):
    """Corsia TENNIS (30/09/2026): oracolo a 2 esiti + telemetria.

    Il ciclo dell'ordine vero e' in `auto_bet` (`_tennis_picks`, corsia LIVE:
    il tennis e' in Denaro Reale). Questo job fa i due pezzi che non hanno
    senso a ogni giro da 60s: (1) aggiorna le cache dell'oracolo Pinnacle a 2
    esiti — **1 credito per torneo con cache scaduta**, budget giornaliero e
    hard-stop; (2) registra la telemetria nel ledger (`predictions` mercato
    `TENNIS`, `match_id` `sx-tennis-*`: il settlement SX-native la salda
    GRATIS). `TENNIS_LANE=0` spegne tutto. Fail-safe: un errore non ferma il
    bot.
    """
    try:
        import tennis_lane
    except Exception as e:                                       # pragma: no cover
        logger.error("tennis_lane_job: modulo non disponibile (%s)", e)
        return
    if not tennis_lane.enabled():
        return
    loop = asyncio.get_running_loop()
    try:
        refreshed = await loop.run_in_executor(_scan_executor,
                                               tennis_lane.refresh_oracle)
        logger.info("tennis_lane_job: oracolo — tornei %s, scaricati %s, "
                    "richieste oggi %s", refreshed.get("keys"),
                    refreshed.get("fetched"), refreshed.get("requests_today"))
    except Exception as e:
        logger.error("tennis_lane_job: refresh oracolo (%s)", e)
    try:
        res = await loop.run_in_executor(_scan_executor, tennis_lane.scan)
        logger.info("tennis_lane_job: %s match, %s candidati, %s registrati",
                    res.get("events"), res.get("candidates"),
                    res.get("registered"))
    except Exception as e:
        logger.error("tennis_lane_job: scan (%s)", e)


async def tennis_quant_job(context: ContextTypes.DEFAULT_TYPE):
    """Motore quantitativo TENNIS (03/10/2026): ELO superficie + Poisson.

    **SOLO MISURA** (scelta esplicita del proprietario): calcola la probabilita'
    intrinseca (ELO superficie-specifico + Poisson da hold/break), la confronta
    col de-vig di Shin dello sharp e col prezzo SX, e registra il verdetto
    "entrambi confermano" in telemetria. NON tocca il denaro: lo stake reale
    resta quello di `auto_bet` (1.50 USDC fissi) e la corsia ordini non importa
    questo modulo.

    Il ciclo gira in un THREAD dell'executor (`run_in_executor`): `run_cycle()`
    e' sincrona e CPU-only (la discovery ha memo 5'), quindi non deve mai essere
    attesa direttamente sul loop. Spento con `TENNIS_QUANT_ENABLED=0`.
    """
    try:
        import tennis_quant
    except Exception as e:                                       # pragma: no cover
        logger.error("tennis_quant_job: modulo non disponibile (%s)", e)
        return
    if not tennis_quant.enabled():
        return
    loop = asyncio.get_running_loop()
    try:
        # Apprendimento dai settlement PRIMA della misura: cosi' il ciclo usa
        # i rating piu' freschi e l'idempotenza (tennis_elo_applied) evita di
        # riapplicare un match gia' contato.
        learned = await loop.run_in_executor(_scan_executor,
                                             tennis_quant.update_ratings_from_ledger)
        if learned.get("applied"):
            logger.info("tennis_quant_job: ELO aggiornato su %s match saldati "
                        "(%s saltati)", learned.get("applied"),
                        learned.get("skipped"))
    except Exception as e:
        logger.error("tennis_quant_job: update rating (%s)", e)
    try:
        res = await loop.run_in_executor(_scan_executor, tennis_quant.run_cycle)
        logger.info("tennis_quant_job: %s eventi, %s valutati, %s candidati "
                    "(entrambi i modelli), %s senza sharp, %s errori",
                    res.get("events"), res.get("evaluated"),
                    res.get("candidates"), res.get("skipped_no_sharp"),
                    res.get("errors"))
    except Exception as e:
        logger.error("tennis_quant_job: ciclo di misura (%s)", e)


async def line_oracle_job(context: ContextTypes.DEFAULT_TYPE):
    """Oracolo a linea OU/AH (30/09/2026): follow-the-money, budget dedicato.

    Scarica `markets=h2h,totals,spreads` di Pinnacle (the-odds-api, 3 crediti
    a chiamata) SOLO per le leghe con pick OU/AH aperti in finestra d'ordine:
    senza questo payload ogni pick a linea muore con `no_oracle` (il gate
    top-down legge un oracolo 1X2 che non ha mai le chiavi 'Over 2.5' /
    'Home -0.75'). Cache separata `toao_<sport>.json` (TTL 24h), budget
    giornaliero `ORACLE_BUDGET_DAY` (default 12 leghe = 36 crediti max/giorno),
    hard-stop crediti rispettato. `ORACLE_ENABLED=0` spegne tutto. Fail-safe:
    un errore non ferma il bot.
    """
    if os.getenv("ORACLE_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return
    loop = asyncio.get_running_loop()

    def _pass():
        import line_oracle
        return line_oracle.ensure_oracle_payloads()

    try:
        res = await loop.run_in_executor(_scan_executor, _pass)
    except Exception as e:
        logger.error("line_oracle_job: %s", e)
        return
    if res.get("fetched") or res.get("errors"):
        logger.info("line_oracle_job: leghe %s, fetch %s, skip %s, errori %s, "
                    "richieste oggi %s", res.get("leagues"),
                    res.get("fetched"), res.get("skipped"),
                    res.get("errors"), res.get("requests_today"))


async def btts_watch_job(context: ContextTypes.DEFAULT_TYPE = None):
    """Sorveglianza GRATUITA del mercato BTTS su SX Bet (25/09/2026).

    Il type 17 (BTTS) e' nella doc ufficiale di SX ma il book NON lo pubblica
    sul calcio (probe reali: 0 mercati al 18/09 e al 25/09/2026, mentre il
    type 2 Over/Under ne pubblica 100+). Senza prezzo non esiste value bet,
    quindi il backlog BTTS e' CONGELATO: nessun feed a pagamento e nessuno dei
    10 punti di refactoring finche' l'exchange non quota il mercato.

    Questo job e' il campanello che lo riapre: una lettura PUBBLICA al giorno
    (zero chiavi, zero crediti the-odds-api, zero ordini) e una notifica agli
    admin SOLO se il feed si popola, con anti-spam 1 alert/giorno (chiave
    BTTS_FEED). Lo stato viene SEMPRE loggato: la telemetria e' continua, la
    notifica e' l'eccezione.
    """
    try:
        import multi_market
        loop = asyncio.get_running_loop()
        probes = await loop.run_in_executor(
            _scan_executor, multi_market.probe_watched_markets)
        line = multi_market.format_probe(probes)
        logger.info("btts_watch: %s", line)
        if not any(p.get("available") for p in probes):
            return
        from datetime import timezone as _tz, timedelta as _td
        from tracker import is_notified, mark_notified
        day = (datetime.now(_tz.utc) + _td(hours=2)).strftime("%Y-%m-%d")
        if is_notified("BTTS_FEED", day):
            return
        mark_notified("BTTS_FEED", day)
        text = ("\U0001f195 MERCATO BTTS DISPONIBILE SU SX\n" + line +
                "\nIl feed ha popolato il type 17: il backlog BTTS puo' "
                "ripartire (nessun feed a pagamento, la quota e' "
                "dell'exchange).")
        if context is not None:
            await _send_report_to_recipients(context, text)
        else:
            logger.warning("btts_watch: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.warning("btts_watch_job fallito: %s", e)


async def history_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Sincronizzazione risultati storici (API-Football) + ricalcolo rating.

    Gira quotidianamente per mantenere aggiornati i dati che alimentano
    rating dinamici e backtest. Se API_FOOTBALL_KEY non e' configurata non
    fa nulla. Usa il default di 1 stagione per restare entro il rate-limit
    del free plan (100 richieste/giorno)."""
    if not os.getenv("API_FOOTBALL_KEY"):
        logger.info("history_sync_job: API_FOOTBALL_KEY assente, salto")
        return
    try:
        res = run_sync(seasons=1)
        logger.info(f"history_sync_job: {res}")
    except Exception as e:
        logger.error(f"Errore history_sync_job: {e}")

def _db_path():
    """Percorso del DB SQLite (vive in DATA_DIR)."""
    return DATA_DIR / "quotaverace.db"


async def tennis_sandbox_job(context: ContextTypes.DEFAULT_TYPE):
    """Sandbox tennis (paper trading) 24/7 — solo SIMULAZIONE su SX Bet.

    Gated da TENNIS_SANDBOX_ENABLED=1 (default off). Ogni 6h: scansione
    +EV dei mercati Moneyline tennis (type 52) via API PUBBLICA SX Bet
    (zero credenziali, zero ordini, zero crediti the-odds-api) e
    settlement dei ghost bet aperti col risultato reale (l'ELO impara
    dalle osservazioni saldate). Notifica solo in caso di attivita'
    (nuovi segnali o settlement), per non fare spam.
    """
    if os.getenv("TENNIS_SANDBOX_ENABLED", "0") != "1":
        return
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            _scan_executor, _run_tennis_sandbox_pass)
    except Exception as e:
        logger.error("tennis_sandbox_job: %s", e)
        return
    if not res:
        return
    signals, settled = res
    if not signals and not settled:
        return
    rows = []
    for s in signals[:5]:
        rows.append(f"  • {s['event']} — {s['selection']} @{s['price']:.2f} "
                    f"(EV {s['ev'] * 100:+.1f}%, stake paper {s['stake']:.2f})")
    for st in settled[:5]:
        outcome = {"won": "✅ vinta", "lost": "❌ persa",
                   "void": "⚪ void"}.get(st["status"], st["status"])
        rows.append(f"  • {st['event']} — {st['selection']}: {outcome} "
                    f"(P/L {st['profit']:+.2f})")
    text = (f"🎾 *SANDBOX TENNIS (paper)* — attività del giro\n"
            f"{len(signals)} nuovi segnali +EV, {len(settled)} settlement\n\n"
            + "\n".join(rows) +
            "\n\n📌 Simulazione: nessun ordine reale, nessun costo.")
    # Direttiva 02/10/2026: nessun invio Telegram per il sandbox tennis.
    # Il dettaglio resta nei log (telemetria intatta); per riattivare la
    # consegna dei messaggi: env TENNIS_SANDBOX_NOTIFY=1.
    logger.info("tennis_sandbox_job: %d segnali, %d settlement "
                "(notifica Telegram disattivata per direttiva 02/10/2026)",
                len(signals), len(settled))
    logger.debug("tennis_sandbox_job dettaglio: %s",
                 text.replace("\n", " | "))


async def tennis_sandbox_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Report giornaliero del sandbox tennis (05:55 UTC): opportunita'
    per giorno, ROI teorico, win rate — per valutare la baseline ELO
    prima di qualsiasi integrazione con denaro reale.
    """
    if os.getenv("TENNIS_SANDBOX_ENABLED", "0") != "1":
        return
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(_scan_executor, _tennis_report_text)
    except Exception as e:
        logger.error("tennis_sandbox_report_job: %s", e)
        return
    if not text:
        return
    # Direttiva 02/10/2026: il report sandbox tennis non si invia piu' su
    # Telegram. Il testo resta calcolato e loggato (telemetria intatta) e
    # l'invio si riattiva con l'env TENNIS_SANDBOX_NOTIFY=1.
    if not _notify_enabled("TENNIS_SANDBOX_NOTIFY"):
        logger.info("tennis_sandbox_report: invio Telegram disattivato "
                    "(TENNIS_SANDBOX_NOTIFY=1 per riattivarlo) — %s",
                    text.replace("\n", " | ")[:500])
        return
    await _send_report_to_recipients(context, text)


def _run_tennis_sandbox_pass():
    """Scan+settle in un colpo (thread dell'executor). Fail-closed."""
    from tennis_sandbox import TennisSandbox
    sb = TennisSandbox()
    try:
        res = sb.scan()
        settled = sb.settle()
        # scan() ritorna il conteggio in `signals` (int) e i DETTAGLI dei
        # segnali del giro in `signal_list` (lista di dict): il job di
        # notifica vuole la LISTA, non il conteggio (fix 08/09: prima qui
        # veniva ritornato l'int e tennis_sandbox_job crashava con
        # "TypeError: 'int' object is not subscriptable" su signals[:5]).
        return res.get("signal_list", []), settled
    finally:
        sb.close()


def _tennis_report_text() -> str:
    """Testo del report giornaliero sandbox tennis (o '' se vuoto)."""
    from tennis_sandbox import TennisSandbox, format_report
    sb = TennisSandbox()
    try:
        rep = sb.report()
    finally:
        sb.close()
    if rep["total_signals"] == 0 and rep["observations"] == 0:
        return ""
    return format_report(rep)


async def backup_data_job(context: ContextTypes.DEFAULT_TYPE):
    """Backup giornaliero dei dati persistenti (delega a backup_manager).

    Snapshot in data/backups/<timestamp>/: DB (consistente via backup API),
    dataset ML fresco (csv+json), cache e log. Tiene gli ultimi BACKUP_KEEP
    snapshot (env, default 7).
    """
    from backup_manager import run_backup
    try:
        await asyncio.get_running_loop().run_in_executor(
            _scan_executor, run_backup)
    except Exception as e:
        logger.error(f"backup_data_job: {e}")


def send_telegram_message_direct(text: str) -> None:
    """Invia un messaggio Telegram usando le credenziali di config."""
    import requests
    # Prova prima TELEGRAM_TOKEN (signals-mvp .env), poi QUOTAVERACE_BOT_TOKEN (Railway)
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("QUOTAVERACE_BOT_TOKEN", "")
    # Il chat di destinazione: su Railway NON esistono TELEGRAM_CHAT_ID* (le
    # variabili locali del vecchio signals-mvp), esiste ADMIN_CHAT_ID. Senza
    # questo fallback il messaggio di avvio non e' MAI stato consegnato in
    # produzione (21/09/2026: warning ripetuto a ogni deploy, messaggio perso).
    _admins = _admin_chat_ids()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID")
               or os.getenv("TELEGRAM_CHAT_ID_FALLBACK")
               or (_admins[0] if _admins else ""))
    if not token or not chat_id:
        logger.warning("Token o chat_id Telegram mancanti, messaggio non inviato")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram errore HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Errore invio Telegram: {e}")

def main() -> None:
    if not TOKEN: raise ValueError("Token non configurato.")
    init_db()
    from auto_bet import kill_switch_status, _execution_mode, t60_window
    from decision.models import Mode
    mode = os.getenv("AUTO_BET_MODE", "sim").strip().lower()
    # Bankroll del messaggio di avvio: SEMPRE un valore reale (equity wallet
    # SX in LIVE, altrimenti la cassa del ledger). Se nessuna delle due fonti
    # risponde il messaggio DICHIARA il problema: il vecchio fallback fisso
    # `BANKROLL_DEFAULT` (100.00) faceva leggere un patrimonio che non
    # esisteva (direttiva 02/10/2026). MAI `get_bankroll()` senza argomento
    # (crash-loop del 19/09/2026).
    bankroll, bankroll_basis = real_bankroll_usdc()
    if bankroll is None:
        bankroll_line = f"💰 <b>Bankroll:</b> ⚠️ {bankroll_basis}"
        logger.error("bankroll di avvio NON disponibile: %s", bankroll_basis)
    else:
        bankroll_line = (f"💰 <b>Bankroll:</b> {bankroll:.2f} USDC "
                         f"<i>({bankroll_basis})</i>")
        logger.info("bankroll reale di avvio: %.2f USDC (%s)",
                    bankroll, bankroll_basis)
    ks = kill_switch_status()
    summary = (
        f"🤖 <b>BOT - QUANT BETTING - SX BET</b>\n"
        f"<b>Avviato in modalità {mode.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Stato circuito:</b> {ks.get('effective', 'unknown')}\n"
        f"🔧 <b>Modalità esecuzione:</b> {_execution_mode()}\n"
        f"⏰ <b>Finestra T-60:</b> {t60_window(None)}\n"
        f"{bankroll_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Sistema pronto. Primo ciclo T-60 tra 60s."
    )
    send_telegram_message_direct(summary)
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("test_segnale", cmd_test_segnale))
    application.add_handler(CommandHandler("segnale", cmd_segnale))
    application.add_handler(CommandHandler("value", cmd_value))
    application.add_handler(CommandHandler("surebet", cmd_surebet))
    application.add_handler(CommandHandler("storico_personale", cmd_storico_personale))
    application.add_handler(CommandHandler("setbankroll", cmd_setbankroll))
    application.add_handler(CommandHandler("campionati", cmd_campionati))
    application.add_handler(CommandHandler("calendario", cmd_calendario))
    application.add_handler(CommandHandler("analisi", cmd_analisi))
    application.add_handler(CommandHandler("schedina", cmd_schedina))
    application.add_handler(CommandHandler("multipla", cmd_multipla))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("premium", cmd_premium))
    application.add_handler(CommandHandler("mytier", cmd_mytier))
    application.add_handler(CommandHandler("checknow", cmd_checknow))
    application.add_handler(CommandHandler("risultati", cmd_risultati))
    application.add_handler(CommandHandler("backtest", cmd_backtest))
    application.add_handler(CommandHandler("backtest_mc", cmd_backtest_mc))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("autobet", cmd_autobet))
    application.add_handler(CommandHandler("hedge", cmd_hedge))
    application.add_handler(CommandHandler("ordini", cmd_ordini))
    application.add_handler(CommandHandler("t60reset", cmd_t60reset))
    application.add_handler(CommandHandler("stopreset", cmd_stopreset))
    application.add_handler(CommandHandler("weeklyreset", cmd_weeklyreset))
    application.add_handler(CommandHandler("settlement", cmd_settlement))
    application.add_handler(CommandHandler("revisioni", cmd_revisioni))
    # Revisioni umane: i bottoni ✅/❌ dei verdetti REVIEW. Il pattern limita
    # l'handler ai NOSTRI callback (`rv:`), cosi' eventuali bottoni di altri
    # messaggi non vengono intercettati.
    application.add_handler(CallbackQueryHandler(review_callback_handler,
                                                pattern=r"^rv:"))
    application.add_handler(CommandHandler("sxscan", cmd_sxscan))
    application.add_handler(CommandHandler("sync", cmd_sync))
    application.add_handler(CommandHandler("quota", cmd_quota))
    application.add_handler(CommandHandler("riepilogo", cmd_riepilogo))
    application.add_handler(CommandHandler("myid", cmd_myid))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))
    if _AI_OK:
        application.add_handler(CommandHandler("ai", cmd_ai))
        logger.info("AI Commander aktif: /ai <pertanyaan>")
    else:
        logger.warning("AI Commander non-aktif: %s", _AI_ERR)
    job_queue = application.job_queue
    if job_queue:
        # I job usano UTC (timezone del container Railway).
        # Per avere gli orari italiani (UTC+2 estive / UTC+1 invernali),
        # soutraiamo 2h (estive) o 1h (invernali). Usa -2 per semplicita'
        # (cambia a -1 a fine ottobre se necessario).
        IT_OFFSET = 2  # UTC+2 (ora legale estiva italiana)
        job_queue.run_daily(morning_job, time=time(hour=6 - IT_OFFSET, minute=0))
        job_queue.run_daily(afternoon_job, time=time(hour=14 - IT_OFFSET, minute=0))
        job_queue.run_daily(evening_job, time=time(hour=20 - IT_OFFSET, minute=0))
        job_queue.run_daily(results_job, time=time(hour=21, minute=30 - IT_OFFSET))
        # Self-healing pendenze: ogni 4h scarica risultati e salda bet/
        # previsioni/cassa rimaste aperte (copre redeploy che saltano i job
        # serali, cache stantie, API lente). Silenzioso se non c'e' nulla.
        # Frequenza 4h (era 2h): risparmio crediti, il referto non serve
        # istantaneo (i risultati serali li coprono i job 21:30/EOD).
        job_queue.run_repeating(settlement_watchdog_job, interval=14400,
                                first=1200)  # primo giro dopo 20 min
        # Riepilogo a fine ultima partita: check ogni 15' dalle 21:00 ITA
        # (fallback notturno 23:50 ITA se la giornata non si chiude da sola).
        job_queue.run_repeating(end_of_day_report_job, interval=900,
                                first=time(hour=21 - IT_OFFSET, minute=0))
        job_queue.run_daily(report_morning_job, time=time(hour=6, minute=5 - IT_OFFSET))
        job_queue.run_daily(history_sync_job, time=time(hour=8, minute=30 - IT_OFFSET))
        # Auto-bet 24/7 (09/09): giro OGNI MINUTO, primo giro 60s dopo il
        # boot — i segnali value/strong_value nuovi (analisi 04:00/12:00/
        # 18:00 UTC, finestra candidati mobile 24h) vengono scommessi entro
        # 1 minuto, giorno e notte. Il giro NON brucia crediti the-odds-api
        # (legge i segnali dal DB e i prezzi SX dall'API pubblica); in LIVE
        # il floor EV riempie solo alla quota-segnale o meglio, quindi la
        # frequenza minuto-per-minuto cattura i miglioramenti di prezzo fino
        # alla guardia dei 15 min pre-kickoff. Sicuro: UNIQUE (match_id,
        # esito) impedisce doppioni e il cap esposizione TOTALE resta
        # giornaliero (sottrae l'esposizione gia' piazzata nei giri
        # precedenti, vedi auto_bet._today_placed_stake). job_kwargs
        # max_instances=1 evita esecuzioni sovrapposte se un giro supera i
        # 60s (in PTB max_instances e' un argomento del Job, non di
        # JobQueue.run_repeating: va passato via job_kwargs).
        # NB: first=60 (delay dopo il boot) e NON first=time(...): con
        # l'orario gia' passato il primo giro slitterebbe al giorno dopo.
        job_queue.run_repeating(auto_bet_job, interval=60,
                                first=60,
                                job_kwargs={"max_instances": 1})
        # Strategia T-60 (17/09): SCANSIONE quotidiana del palinsesto (i giri
        # analisi/quote restano) + DECISIONE ESECUTIVA nella finestra T-60..T-50
        # prima del fischio: questo job ogni 60s esegue SOLO dentro finestra
        # (cap 1 USDC/ordine, kill switch 30 USDC, contratto Pydantic, gate
        # mercato). Il watch CB2 ogni 6h rende visibile l'arresto.
        job_queue.run_repeating(t60_job, interval=60, first=75,
                                job_kwargs={"max_instances": 1})
        job_queue.run_repeating(t60_kill_watch_job, interval=6 * 3600,
                                first=600, job_kwargs={"max_instances": 1})
        # Scan SX Bet (09/09): segnali value 1X2 SOLO dai prezzi SX (API
        # pubblica, zero crediti the-odds-api) + settlement bet sx-*. Ogni
        # 15 min (SX_SCAN_INTERVAL_MIN): l'auto-bet (ogni minuto) piazza al
        # giro successivo i segnali nuovi. SX_SIGNALS_ENABLED=0 per spegnerlo.
        _sx_min = max(5, int(os.getenv("SX_SCAN_INTERVAL_MIN", "15")))
        job_queue.run_repeating(sx_signals_job, interval=_sx_min * 60,
                                first=90,
                                job_kwargs={"max_instances": 1})
        # Corsia multi-mercato OU/AH (19/09): stesso intervallo dello scan
        # 1X2 (default 15'), ingest + analisi Poisson + ledger. AH live, OU
        # shadow. MM_ENABLED=0 per spegnerla senza toccare il 1X2.
        job_queue.run_repeating(multi_market_job, interval=_sx_min * 60,
                                first=150,
                                job_kwargs={"max_instances": 1})
        # Telemetria ombra mercati NON calcistici (01/10): scrive SOLO su
        # `market_quotes` (mai predictions). Spenta di default
        # (SHADOW_MARKET_ENABLED=1 per accenderla): a codice invariato non
        # parte nessun ciclo e il costo resta zero.
        job_queue.run_repeating(market_shadow_job, interval=_sx_min * 60,
                                first=210,
                                job_kwargs={"max_instances": 1})
        # Corsia TENNIS (30/09/2026): oracolo a 2 esiti + telemetria. Ogni 6h
        # (l'oracolo ha TTL 12h e 3 tornei attivi: ~6 crediti/giorno). Gli
        # ORDINI tennis sono nella corsia LIVE di auto_bet (`_tennis_picks`),
        # quindi questo job non decide nulla sul denaro.
        _tennis_min = max(60, int(os.getenv("TENNIS_JOB_INTERVAL_MIN", "360")))
        job_queue.run_repeating(tennis_lane_job, interval=_tennis_min * 60,
                                first=330,
                                job_kwargs={"max_instances": 1})
        # Motore quantitativo TENNIS (03/10/2026): ELO superficie + Poisson +
        # Shin in MISURA. Stesso intervallo della corsia tennis (i rating si
        # aggiornano coi settlement, la discovery ha memo 5'). Spento con
        # TENNIS_QUANT_ENABLED=0; nessun ordine, nessun credito (l'oracolo
        # sharp e' gia' in cache e la discovery SX e' pubblica).
        job_queue.run_repeating(tennis_quant_job, interval=_tennis_min * 60,
                                first=390,
                                job_kwargs={"max_instances": 1})
        # Copertura intelligente (26/09): ogni 15' (stesso intervallo dello
        # scan multi-mercato) valuta le posizioni LIVE aperte e piazza le
        # coperture a profitto bloccato. SMART_HEDGING=0 per spegnerlo.
        job_queue.run_repeating(hedge_job, interval=_sx_min * 60,
                                first=210,
                                job_kwargs={"max_instances": 1})
        # Sorveglianza BTTS (25/09): il type 17 non e' pubblicato sul calcio
        # (0 mercati), il backlog BTTS e' congelato. Una lettura pubblica al
        # giorno — zero crediti, zero ordini — avvisa gli admin se il feed si
        # popola: e' il campanello che riapre il refactoring.
        job_queue.run_repeating(btts_watch_job, interval=24 * 3600,
                                first=1800,
                                job_kwargs={"max_instances": 1})
        # Oracolo a linea OU/AH (30/09/2026): follow-the-money. Ogni 30'
        # scarica totals/spreads SOLO per le leghe con pick a linea in gioco
        # (budget dedicato 12 leghe/giorno): e' il pezzo che sblocca l'AH/OU
        # dal no_oracle del gate top-down. ORACLE_ENABLED=0 per spegnerlo.
        job_queue.run_repeating(line_oracle_job, interval=1800, first=270,
                                job_kwargs={"max_instances": 1})
        # Revisioni umane (15/09): i verdetti `review` della catena diventano
        # prompt Telegram con bottoni. Frequenza 5 min (non c'e' fretta: il
        # prompt serve prima del kickoff, e l'invio e' idempotente) e marker
        # sullo store per non rimandare lo stesso segnale a ogni giro.
        job_queue.run_repeating(decision_review_job,
                                interval=REVIEW_JOB_INTERVAL_SECONDS, first=240,
                                job_kwargs={"max_instances": 1})
        job_queue.run_daily(backup_data_job, time=time(hour=3, minute=30))
        job_queue.run_once(backup_data_job, when=10)  # snapshot di base all'avvio
        # Retrain ensemble ML dal ledger live (05:45 UTC + a ogni boot): se
        # il dataset e' maturo crea/aggiorna data/ensemble_model.json sul
        # volume (senza questo job il ML resterebbe spento in produzione:
        # nessun file, nessuna predizione ensemble). Poi azzera la cache del
        # singleton. Il run_once al boot (09/09) fa si' che la calibrazione
        # isotonica (soglia MIN_CALIB_SAMPLES abbassata a 50) si attivi
        # SUBITO al primo deploy che la introduce, senza attendere le 05:45
        # del giorno dopo; il retrain e' idempotente e non tocca l'API.
        job_queue.run_daily(retrain_ensemble_job, time=time(hour=5, minute=45))
        job_queue.run_once(retrain_ensemble_job, when=20)  # retrain al boot
        # Drift watchdog (09/09): monitoraggio CONTINUO in background della
        # calibrazione rolling ogni 6h. Alerta admin+iscritti SOLO quando il
        # drift e' rilevato (anti-spam: massimo 1 alert/24h a drift
        # persistente), stato sempre loggato. Il retraining e' gia' coperto
        # dal job 05:45 UTC + boot: questo job e' il campanello automatico.
        job_queue.run_repeating(drift_watchdog_job, interval=6 * 3600,
                                first=1800)
        # Credit watchdog (09/09): monitoraggio ogni 6h.
        # Alerta admin+iscritti sotto le soglie (50, 20, 10, 5).
        # Zero API cost: legge solo le cache toa_*.json.
        job_queue.run_repeating(credit_watchdog_job, interval=6 * 3600,
                                first=300)
        # Monitor scarti liquidita' SX (11/09): ogni 6h legge il log degli
        # scarti per book sottile (scan/order/partial) e allerta admin+
        # iscritti SOLO se ce ne sono nelle ultime 24h (anti-spam 1/giorno).
        # Zero costi API: legge solo il JSONL sul volume.
        job_queue.run_repeating(liquidity_monitor_job, interval=6 * 3600,
                                first=600)
        # Verifica degli ORDINI REALI (28/09): ogni 30' controlla stake fisso
        # (1.50) e recinto del 40% sull'esposizione aperta. Sola lettura del
        # ledger, zero crediti; allerta SOLO sulle violazioni (1/giorno).
        # Piu' frequente degli altri monitor perche' un ordine fuori regola va
        # visto adesso, non fra 6 ore.
        job_queue.run_repeating(order_watch_job, interval=1800, first=420)
        # Flusso dell'order book SX (26/09): ogni 6h legge il registro degli
        # ingressi di liquidita' e allerta SOLO se ce ne sono nelle ultime
        # 24h (anti-spam 1/giorno). TELEMETRIA: zero costi API, zero ordini.
        job_queue.run_repeating(book_flow_job, interval=6 * 3600, first=750)
        # Confronto shadow catena ↔ corsia (16/09): ogni 6h confronta il ledger
        # delle decisioni con quello delle puntate (sola lettura, zero costi) e
        # allerta SOLO gli admin, 1 volta/giorno, quando ci sono divergenze.
        # E' la misura su cui si decidera' il passo 3 (sostituire l'esecuzione).
        job_queue.run_repeating(decision_compare_job, interval=6 * 3600,
                                first=900, job_kwargs={"max_instances": 1})
        # Sandbox tennis (paper trading, 08/09): SOLO simulazione su SX Bet
        # (mercati Moneyline type 52, letture pubbliche gratuite). Gated da
        # TENNIS_SANDBOX_ENABLED=1: scan+settle ogni 6h (primo giro 15 min
        # dopo il boot) + report giornaliero 05:55 UTC. Mai ordini reali:
        # il modulo non ha credenziali e non importa da tracker/bot.
        if os.getenv("TENNIS_SANDBOX_ENABLED", "0") == "1":
            job_queue.run_repeating(tennis_sandbox_job, interval=6 * 3600,
                                    first=900)
            job_queue.run_daily(tennis_sandbox_report_job,
                                time=time(hour=5, minute=55))
            logger.info("Sandbox tennis abilitato (paper trading, 6h + report 05:55 UTC)")
        # Piano free: riceve gli stessi segnali con 3 ore di ritardo.
        job_queue.run_daily(free_delayed_job, time=time(hour=17 - IT_OFFSET, minute=0))
        # Alert RLM real-time: ogni 5 minuti dalle 14:00 alle 23:50 ITA
        try:
            from rlm_alert import rlm_alert_job
            job_queue.run_repeating(rlm_alert_job, interval=300,
                                    first=time(hour=14 - IT_OFFSET, minute=0))
        except ImportError:
            logger.warning("rlm_alert non disponibile, alert RLM disabilitato")
        logger.info("Job Pro schedulati (ora italiana): 03:30 backup / 05:55 sandbox tennis "
                    "(report) / 06:05 riepilogo ieri / "
                    "08:30 sync / auto-bet 24/7 (ogni minuto) / 14:00 pomeriggio / "
                    "14:00-23:50 RLM alert (5') / 17:00 free / 20:00 sera / "
                    "21:30 risultati / 21:00-23:50 EOD (ogni 15') / "
                    "watchdog settlement (ogni 4h) / retrain ensemble ML "
                    "(05:45 + boot) / drift watchdog (ogni 6h)")
    else: logger.warning("JobQueue non disponibile")
    logger.info("QuotaVerace Pro avviato.")
    application.run_polling()

if __name__ == "__main__":
    main()
