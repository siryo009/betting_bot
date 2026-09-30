"""auto_bet.py — Puntate automatiche giornaliere (SIM oppure LIVE via engine).

DAL 04/09 le puntate automatiche sono state rese SIMULATE (paper trading
con la quota del segnale) per alimentare ledger, CLV e dataset ML senza
conto Exchange. DAL 08/09 auto_bet puo' piazzare ORDINI REALI collegando i
segnali value a `execution_engine.py` (provider SX Bet V3, primo provider
abilitato, poi Smarkets/aggregatori): la modalita' si attiva con
`AUTO_BET_MODE=live` (o `real`) E un provider reale configurato
(EXECUTION_PROVIDER + credenziali). Senza quelle condizioni resta SIM
(default sicuro) — oppure non piazza nulla se il chiamante richiede il
fail-closed (allow_sim=False).

Flusso del mattino (job 08:50 UTC, dopo analisi):

1. Legge i segnali value/strong_value del giorno da match_analysis (quelli
   che battono il mercato, come la schedina);
2. Per ogni segnale calcola lo stake: ADATTIVO di default (Kelly
   frazionato dinamico con drawdown protection e confidence weighting)
   oppure FLAT (AUTO_BET_STAKE_MODE=flat, 1 USDC per segno dal 09/09)
   con risk cap a unita' intere;
3. Esegue: in LIVE risolve il mercato dell'exchange (evento+esito),
   verifica che il prezzo disponibile sia >= quota del segnale (floor EV:
   mai riempirsi sotto la quota su cui e' stato calcolato l'edge) e piazza
   l'ordine; in SIM registra la puntata simulata con la quota del segnale;
4. Registra la puntata nella tabella `bets` (mode='live' con market_id /
   selection_id / bet_id reali, mode='sim' altrimenti) per tracking,
   settlement e riepilogo di fine giornata (settle_bets la salda come
   sempre). Un ordine reale NON riempito o saltato non lascia righe.

Regole prudenti di esecuzione (scelte per questo progetto):
- si scommette SOLO su segnali value/strong_value che battono il mercato;
- si salta una partita se manca < 15 minuti al calcio d'inizio;
- una sola puntata per (match, esito): la UNIQUE(match_id, esito) in `bets`
  impedisce di raddoppiare se il job viene rilanciato;
- correlation risk cap (30% bankroll per blocco correlato) + cap esposizione
  totale del giorno (40% bankroll), applicati PRIMA di salvare;
- in LIVE il market dell'exchange deve riferirsi alla STESSA partita del
  segnale (nomi squadre + kickoff): nessun ordine su eventi ambigui
  (fail-closed), e nessun riempimento sotto la quota-segnale (floor EV).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from config import DATA_DIR

logger = logging.getLogger("auto_bet")

BET_STAKE_DEFAULT_EUR = 5.0
MIN_MINUTES_TO_START = 15

# --- Esecuzione reale via execution_engine (wiring dal 08/09) ---
# AUTO_BET_MODE=live|real -> ordini REALI sul provider configurato
# (EXECUTION_PROVIDER=sxbet|smarkets|betinasia|mollybet + credenziali).
# Qualunque altro valore (default) -> SIM. Se il chiamante passa
# allow_sim=False e il live non e' disponibile, non si piazza nulla
# (fail-closed).
REAL_MODE_VALUES = ("live", "real", "1", "true", "on")

# --- Kill-switch Telegram (dal 08/09) ---
# Override persistente scritto dal comando /autobet: il file vive sul volume
# condiviso (data/execution/auto_bet_mode.json) quindi sopravvive ai
# redeploy. Valori:
#   "off"  -> STOP TOTALE: nessuna puntata (ne' reale ne' simulata);
#   "sim"  -> PAUSA ordini reali: resta solo il paper trading;
#   "live" -> ripristina AUTO_BET_MODE env (nessun override).
# In assenza del file vale AUTO_BET_MODE env (default "sim").
KILL_SWITCH_FILE = DATA_DIR / "execution" / "auto_bet_mode.json"
KILL_SWITCH_VALUES = ("off", "sim", "live")

# --- Stop-loss giornaliero (11/09/2026) ---
# Taglia le perdite durante i "giorni neri": se il bankroll scende di
# DAILY_STOP_LOSS_PCT (default 5%) rispetto al valore registrato a INIZIO
# giornata, le puntate vengono BLOCCATE per DAILY_STOP_HOURS (default 24h).
# Lo stato vive sul volume (data/execution/daily_stop.json) e sopravvive ai
# redeploy; si ri-arma al primo giro del giorno successivo.
# In LIVE il valore di riferimento e' l'EQUITY del wallet — disponibile PIU'
# fondi in gioco (escrow) — non il solo `availableBalance`: piazzare una bet
# sposta i fondi da "disponibile" a "in gioco" senza che nulla sia stato
# perso, e misurare il rischio sul solo disponibile faceva scattare lo stop
# per un -5% che era aritmetica, non denaro (bug fixato il 15/09/2026:
# l'equity di un wallet 35.98 con 2.0 in gioco e 33.98 liberi resta 35.98).
# In SIM resta la cassa. Env: DAILY_STOP_LOSS_PCT, DAILY_STOP_HOURS.
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", "0.05"))
DAILY_STOP_HOURS = float(os.getenv("DAILY_STOP_HOURS", "24"))
DAILY_STOP_FILE = DATA_DIR / "execution" / "daily_stop.json"

# --- Risk Management & Circuit Breaker SETTIMANALE (26/09/2026) ---
# Direttiva: drawdown ROLLING su 7 giorni (168h). Se l'equity scende di
# WEEKLY_STOP_LOSS_PCT (default 12%) rispetto al PICCO delle ultime
# WEEKLY_STOP_WINDOW_H ore, le puntate si bloccano per WEEKLY_STOP_HOURS
# (default 24h) e si ri-armano da sole quando il drawdown rientra (il picco
# vecchio esce dalla finestra). Lo storico e' compattato su volume (un
# campione/ora, `WEEKLY_SAMPLE_MIN_SECONDS`) e sopravvive ai redeploy; come lo
# stop giornaliero si misura sull'EQUITY (disponibile + in gioco) in LIVE e
# sulla cassa in SIM, senza mai confrontare basi diverse.
WEEKLY_STOP_LOSS_PCT = float(os.getenv("WEEKLY_STOP_LOSS_PCT", "0.12"))
WEEKLY_STOP_HOURS = float(os.getenv("WEEKLY_STOP_HOURS", "24"))
WEEKLY_STOP_WINDOW_H = float(os.getenv("WEEKLY_STOP_WINDOW_H", "168"))
WEEKLY_STOP_FILE = DATA_DIR / "execution" / "weekly_stop.json"
BANKROLL_HISTORY_FILE = DATA_DIR / "execution" / "bankroll_history.json"
WEEKLY_SAMPLE_MIN_SECONDS = float(os.getenv("WEEKLY_SAMPLE_MIN_SECONDS", "3600"))

# --- Correlation risk cap ---
# Kelly assume indipendenza tra le puntate: due o piu' esiti correlati nello
# stesso blocco temporale (stessa partita, stessa lega con kickoff ravvicinati)
# moltiplicano la varianza reale. Ogni stake viene ridotto proporzionalmente
# quando l'esposizione totale del blocco supera il cap.
CORRELATION_CAP_PCT = 0.30     # max 30% di bankroll per blocco correlato
CORRELATION_WINDOW_MIN = 90    # kickoff entro 90' = stesso blocco temporale
# Cap di portafoglio: esposizione TOTALE del giorno (somma di tutti gli
# stake) <= 40% del bankroll. Kelly dimensiona ogni stake singolarmente;
# senza questo cap, 5+ segnali indipendenti sommano comunque un rischio
# complessivo che cresce col numero di pick (varianza additiva).
TOTAL_EXPOSURE_CAP_PCT = 0.40  # max 40% di bankroll per il portafoglio del giorno

# --- RECINTO DI CAPITALE (direttiva 27/09/2026) -----------------------------
# Il cap di portafoglio sopra misura i FLUSSI del giorno; questo misura
# l'ESPOSIZIONE APERTA, cioe' il capitale immobilizzato nelle puntate reali
# non ancora saldate (escrow): e' il numero che descrive davvero quanti soldi
# sono in gioco. Le due soglie sono complementari, non alternative.
# Quando l'esposizione aperta >= OPEN_EXPOSURE_CAP_PCT del bankroll il bot
# DEGRADA a shadow: nessun nuovo ordine REALE, telemetria e valutazione
# (corsia + catena) continuano a girare. Si sblocca da sola quando i
# settlement chiudono le righe e l'esposizione torna sotto soglia.
OPEN_EXPOSURE_CAP_PCT = float(os.getenv("OPEN_EXPOSURE_CAP_PCT", "0.40"))
# Tetto per SINGOLO ordine (micro-stake): l'esposizione va spalmata su piu'
# partite, non concentrata su una. Vale per ogni corsia (quella storica e la
# catena piramidale): e' un tetto ASSOLUTO, mai un floor.
ORDER_MAX_STAKE_USDC = float(os.getenv("ORDER_MAX_STAKE_USDC", "1.50"))
# --- STAKE FISSO (direttiva 28/09/2026) ------------------------------------
# La size di ogni singola scommessa REALE non e' piu' dimensionata (Kelly +
# cap): e' un IMPORTO FISSO, 1.50 USDC. Con 8 ordini aperti l'esposizione e'
# 12.00 USDC, sotto il recinto del 40%. Il valore resta comunque soggetto al
# tetto per-ordine qui sopra (l'env puo' abbassare l'importo, mai alzarlo
# oltre 1.50) e al vincolo di cassa (non si spendono fondi che non ci sono).
# `ORDER_FIXED_STAKE_USDC=0` ripristina lo staking dinamico storico.
FIXED_STAKE_USDC = float(os.getenv("ORDER_FIXED_STAKE_USDC", "1.50"))
# Modalita' della catena piramidale dentro il giro REALE:
#   "off"   (default) -> la catena registra/valuta, non esegue (Fase 1/2);
#   "live"            -> i piani approvati dal Finance Agent entrano nella
#                        STESSA coda di esecuzione della corsia storica, con
#                        T-60, liquidita', oracolo top-down, cap e ledger
#                        condivisi: non esiste un secondo canale di denaro.
# La decisione di eseguire resta SEMPRE del Capo (verdetto `approve` +
# stake eseguibile): qui si regola solo se quei piani possono diventare
# ordini reali invece che registri shadow.
CHIEF_EXECUTION = (os.getenv("CHIEF_EXECUTION", "off").strip().lower()
                   or "off")

# Staking 100% dinamico (08/09): NESSUN importo fisso. Lo stake lo decide
# il Kelly frazionato sul bankroll corrente (saldo reale del wallet in
# LIVE). Restano solo due vincoli di sicurezza:
#   - STAKE_STEP_EUR (default 0.01): arrotondamento fine, niente step fissi;
#   - MIN_STAKE_EUR (default 1.0): floor dell'exchange (SX Bet: 1 USDC).
STAKE_STEP_EUR = float(os.getenv("STAKE_STEP_EUR", "0.01"))
MIN_STAKE_EUR = float(os.getenv("MIN_STAKE_EUR", "1.0"))

# CAP SEVERO OBBLIGATORIO (11/09/2026): il cap per singola bet (1% value/
# moderate, 2% strong_value) NON puo' MAI essere superato dal floor
# dell'exchange. Con STAKE_CAP_HARD attivo (default) una bet il cui stake
# cappato e' sotto il minimo ordine (1 USDC) viene SALTATA (fail-closed)
# invece di essere alzata al floor: cosi' il cap e' vero, non cosmetico.
# Conseguenza operativa: con bankroll < 100 USDC il cap 1% e' sotto 1 USDC,
# quindi nessun ordine parte finche' il wallet non cresce (>= 100 USDC per
# il cap 1%, >= 50 per il 2%) oppure finche' non si accetta il floor con
# STAKE_CAP_HARD=0.
STAKE_CAP_HARD = os.getenv("STAKE_CAP_HARD", "1").strip().lower() \
    in ("1", "true", "yes", "on")

# --- STRATEGIA T-60 + CIRCUIT BREAKERS (direttiva del proprietario,
# 17/09/2026) ------------------------------------------------------------
# La finestra esecutiva di una partita si apre 60 minuti prima del fischio
# (T-60) e si chiude 50 minuti prima (T-50): dentro quella finestra il giro
# valuta i segnali validati e dispaccia gli ordini reali, con micro-
# allocazioni (cap per ordine) e limiti di esposizione rigorosi. Fuori
# finestra la strategia NON ordina: i segnali vengono scansionati e
# classificati dai giri normali (che restano ogni 60s), la decisione
# esecutiva arriva alla T-60.
T60_WINDOW_MIN_MIN = float(os.getenv("T60_WINDOW_MIN_MIN", "60"))   # apertura (minuti al kickoff)
# CHIUSURA (minuti al kickoff). 15 dal 30/09/2026 (era 50): l'esecuzione
# arriva fino a T-15 per rendere ORDINABILI i ritentativi tardivi dell'oracolo
# eSports: Pinnacle pubblica tardi sugli eSports. Il TTL-miss (15min) e la finestra
# dell'oracolo (1h) sono tarati su QUESTO bordo: con la chiusura a T-50 i
# tentativi a T-45/T-30/T-15 avrebbero consumato quota per pick che il gate
# T-60 avrebbe poi rifiutato come `missed` (budget bruciato a vuoto).
# ⚠️ Il pavimento assoluto resta `MIN_MINUTES_TO_START` (15 min), che salta
# comunque le partite che stanno per iniziare: le due guardie COINCIDONO.
T60_WINDOW_MAX_MIN = float(os.getenv("T60_WINDOW_MAX_MIN", "15"))   # chiusura (fail-closed: oltre, non si ordina)
# CB1 — HARD CAP PER ORDINE: NESSUN calcolo dinamico (Kelly incluso) puo'
# produrre uno stake sopra questo tetto: viene SORSCRITTO. Il proprietario
# ha scelto 1.00 USDC (17/09): e' il minimo ordine eseguibile dell'exchange
# SX Bet (con 0.50 OGNI ordine sarebbe stato scartato dal floor e il
# sistema sarebbe rimasto armato ma inerte). Env T60_MAX_STAKE_USDC per
# tornare alla direttiva letterale (0.50) se il minimo exchange cambia.
T60_MAX_STAKE_USDC = float(os.getenv("T60_MAX_STAKE_USDC", "1.00"))
# Tetto quota della strategia (favoriti netti, allineato a value_filter): un
# ordine su una quota fuori fascia e' dati incoerenti, non un mercato.
T60_MAX_ODDS = float(os.getenv("T60_MAX_ODDS", "1.80"))
# CB2 — KILL SWITCH PATRIMONIALE: se l'equity del wallet scende a questa
# soglia o sotto, il sistema si ARRESTA (stop job + alert di emergenza
# Telegram, antirumore 1/giorno). Persistente sul volume (sopravvive ai
# redeploy) e fail-closed: un errore di lettura del wallet e' un blocco,
# non un via libera. Reset manuale: /t60reset (admin) o rimozione del file.
T60_KILL_WALLET_USDC = float(os.getenv("T60_KILL_WALLET_USDC", "30.0"))
T60_KILL_FILE = DATA_DIR / "execution" / "t60_kill.json"
# CB3 — VALORIZZAZIONE PYDANTIC RIGIDA: ogni payload d'ordine passa dal
# contratto `decision.models.T60OrderContract` PRIMA del dispatch; un
# payload malformato viene scartato e registrato nel ledger SQLite (bets
# mode='rejected-t60'). Env: disattivabile SOLO in via eccezionale (i test
# e la diagnostica la tengono SEMPRE attiva).
T60_ORDER_VALIDATION = os.getenv("T60_ORDER_VALIDATION", "1").strip().lower() \
    in ("1", "true", "yes", "on")
# La corsia d'ordine del giro normale respecta la finestra T-60: fuori
# finestra il palinsesto viene solo SCANSIONATO e classificato (telemetria
# completa), la decisione esecutiva arriva nella finestra. T60_EXECUTION_ONLY=0
# ripristina il comportamento pre-T60 (ordini in tutto l'orizzonte 0.5-24h).
T60_EXECUTION_ONLY = os.getenv("T60_EXECUTION_ONLY", "1").strip().lower() \
    in ("1", "true", "yes", "on")

# --- STRATEGIA TOP-DOWN "STEAM CHASING" (fase 2, direttiva del proprietario
# 25/09/2026) -------------------------------------------------------------
# Il modello bottom-up (Poisson) resta a monte di tutto (genera i candidati
# e popola il ledger), ma la VALUTAZIONE EV del giro ordini passa
# all'ORACOLO: dal 26/09 non il prezzo secco di UNA fonte, ma il CONSENSO
# de-vigato multi-fonte (Pinnacle + Betfair Exchange come benchmark,
# Matchbook come validatore; fallback automatico alle fonti presenti, fino
# alla sola Pinnacle). La quota del segnale (SX) e' il prezzo, e si compra
# solo il ritardo fra i due. Lettura DALLE CACHE che la rotazione quote
# scarica gia': ZERO crediti. Con TOP_DOWN_EV attivo, un segnale senza
# oracolo e' "no_oracle" e NON viene ordinato (fail-closed).
TOP_DOWN_EV = os.getenv("TOP_DOWN_EV", "1").strip().lower() \
    in ("1", "true", "yes", "on")
#: Moltiplicatore della quota equa di Pinnacle ("true odd + margine"): la
#: stessa condizione del gate EV scritta come prezzo minimo
#: (EV >= ev_min  <=>  quota >= true_odd x (1 + TOP_DOWN_MARGIN)).
TOP_DOWN_MARGIN = float(os.getenv("TOP_DOWN_MARGIN", "0.02"))
#: Dry-Run (25/09): calcola EV e logga i candidati che superano la soglia,
#: ma intercetta l'ordine PRIMA della chiamata POST a SX Bet. Env
#: AUTO_BET_DRY_RUN=1 (o argomento dry_run=True). Il resto del giro e'
#: INVARIATO: guardrail, gate di mercato, risk cap e log dei candidati
#: girano identici — solo l'effetto sul mondo e' soppresso.
DRY_RUN = os.getenv("AUTO_BET_DRY_RUN", "0").strip().lower() \
    in ("1", "true", "yes", "on")


def t60_window(kickoff: "datetime | None") -> str:
    """Posizione di un kickoff rispetto alla finestra esecutiva T-60.

    Ritorna: "before" (kickoff oltre T-60: non ancora), "within" (dentro
    T-60..T-50: finestra esecutiva), "missed" (meno di T-50: non si ordina,
    fail-closed), "unknown" (kickoff non parsabile: non si ordina).
    """
    if kickoff is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    k = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=timezone.utc)
    mins = (k - now).total_seconds() / 60.0
    if mins > T60_WINDOW_MIN_MIN:
        return "before"
    if mins >= T60_WINDOW_MAX_MIN:
        return "within"
    return "missed"


def t60_stake(bankroll: float, *, mode: str = "sim") -> float:
    """Stake T-60: CB1 HARD CAP 1.00 USDC, NESSUN Kelly.

    Micro-allocazione FISSA per la strategia T-60 (direttiva 17/09): il
    Kelly dinamico e' ignorato di proposito — qualunque calcolo che produca
    uno stake superiore al cap viene sovrascritto. Rispetta comunque i
    limiti di portafoglio (correlazione 30%, esposizione totale 40%) e la
    cassa reale del wallet: mai un ordine sopra i fondi disponibili.
    """
    try:
        bankroll = float(bankroll)
    except (TypeError, ValueError):
        bankroll = 0.0
    stake = min(float(T60_MAX_STAKE_USDC),
                bankroll * CORRELATION_CAP_PCT,
                bankroll * TOTAL_EXPOSURE_CAP_PCT)
    if mode == "live":
        stake = min(stake, bankroll)   # mai oltre la cassa reale
    if stake < MIN_STAKE_EUR:
        return 0.0                     # sotto il minimo ordine: no bet
    return float(round(min(stake, T60_MAX_STAKE_USDC), 2))


# Loader dell'oracolo INIETTABILE (default = le cache di produzione): i test
# lo sostituiscono senza toccare la rete, e un giorno un feed alternativo
# (live diagnostica) entra da qui senza riscrivere la valutazione.
_TOP_DOWN_CACHE_DIR = None

#: Corsia TOP-DOWN (25/09, direttiva "andiamo live subito"): la corsia LIVE
#: non eredita il filtro bottom-up (fascia favoriti 1.30-1.80, tier
#: value/strong_value): il candidato nasce da OGNI riga 1X2 aperta della
#: finestra mobile 24h e l'unico giudice del prezzo e' l'oracolo Pinnacle
#: (EV >= EV_MIN, fail-closed senza oracolo).
#:
#: DEFAULT 0 (SPENTO) dal 26/09/2026: il bypass NON governa gli ordini reali
#: finche' non lo si accende esplicitamente con `TOP_DOWN_BYPASS=1` (env
#: Railway, dichiarata in `preserve()`). Il default era ON nella bozza del
#: 25/09: con `AUTO_BET_DRY_RUN` a 0 significava ordini reali che scavalcano
#: il gate di prezzo 1.30-1.80 senza una decisione esplicita.
TOP_DOWN_BYPASS = os.getenv("TOP_DOWN_BYPASS", "0").strip().lower() \
    in ("1", "true", "yes", "on")

#: Gate di lega della corsia top-down: in probation l'oracolo DEVE essere
#: MIGLIORE del segnale per passare (EV >= EV_MIN + TOP_DOWN_PROBATION_EXTRA).
TOP_DOWN_PROBATION_EXTRA = float(
    os.getenv("TOP_DOWN_PROBATION_EXTRA", "0.02"))


def _top_down_load(home: str, away: str):
    """p_true per esito dall'oracolo Pinnacle (cache, 0 crediti)."""
    import pinnacle_oracle as po
    return po.load_oracle(home, away, cache_dir=_TOP_DOWN_CACHE_DIR)


def _top_down_eval(pick: dict, league: str | None = None) -> dict | None:
    """Valutazione TOP-DOWN di un candidato: EV contro l'ORACOLO Pinnacle.

    Fase 2 del pivot (25/09/2026): la p_true NON arriva dal modello di gol
    (Poisson/blend), ma dal mercato sharp de-vigato, letto dalle CACHE che
    la rotazione quote scarica gia' (`pinnacle_oracle.load_oracle`, ZERO
    crediti). La quota del segnale (SX) resta il prezzo. Si compra solo il
    ritardo fra Pinnacle e SX: `EV = p_true x (quota - 1) - (1 - p_true)`.

    La soglia EV e' UNA: `value_filter.EV_MIN` di produzione, confrontata
    alla soglia dichiarata dall'oracolo (`pinnacle_oracle.DEFAULT_EV_MIN` e'
    la STESSA costante importata la') — niente doppio standard fra i due
    percorsi. Il margine del prezzo minimo (`TOP_DOWN_MARGIN`, default 2%)
    e' la seconda forma della stessa condizione: `quota >= true_odd x
    (1 + margine)`. Un'eventuale divergenza fra le due letture e' un bug
    del gate, non una soglia nuova.

    Corsia top-down (25/09, `TOP_DOWN_BYPASS`): nelle leghe in PROBATION
    l'oracolo deve battere il segnale di un extra (`TOP_DOWN_PROBATION_EXTRA`,
    default +2pp di EV): il filtro di prezzo e' bypassato, la prudenza sulle
    leghe non ancora validate no (direttiva anti-spread 21/09). `league` e'
    opzionale: None = nessun extra (retrocompatibile con chiamanti e test).

    Ritorna un dict con verdetto e diagnostica; mai eccezioni verso il
    chiamante (un errore della valutazione e' un salto, non un crash).
    """
    try:
        import pinnacle_oracle as po
        probs = _top_down_load(pick.get("home") or "", pick.get("away") or "")
    except Exception as e:
        return {"ok": False, "reason": f"oracolo non disponibile ({e})"}
    try:
        quota = float(pick.get("quota") or 0)
        if quota <= 1.0:
            return {"ok": False, "reason": "quota non valida"}
        if not probs:
            return {"ok": False, "reason": "no_oracle",
                    "detail": "Pinnacle assente/incompleto/stantio "
                              "(fail-closed: senza verita' non si decide)"}
        p_true = float(probs.get(pick.get("esito_key")) or 0)
        if not (0.0 < p_true <= 1.0):
            return {"ok": False, "reason": "no_oracle",
                    "detail": f"esito '{pick.get('esito_key')}' senza "
                              "probabilita' fair"}
        ev = p_true * (quota - 1.0) - (1.0 - p_true)
        true_odd = 1.0 / p_true
        try:
            from value_filter import EV_MIN as ev_min
        except Exception:                                       # pragma: no cover
            ev_min = po.DEFAULT_EV_MIN
        # Probation: soglia EV piu' severa per le leghe non ancora validate
        # (il filtro di prezzo e' bypassato, la prudenza no — direttiva
        # anti-spread del 21/09). Il tier arriva da league_tier (normalizza
        # anche il nome: lezione 24/09).
        ev_min_eff = float(ev_min)
        if league:
            try:
                from value_filter import league_tier
                if league_tier(league) == "probation":
                    ev_min_eff += max(0.0, float(TOP_DOWN_PROBATION_EXTRA))
            except Exception:                                   # pragma: no cover
                pass
        required = true_odd * (1.0 + ev_min_eff)
        # Le due letture della stessa condizione: se divergono, e' un bug del
        # gate (una sola soglia EV nel sistema) — si logga, non si "sistema".
        if abs((quota >= required) - (ev >= ev_min_eff)) > 1e-12:
            logger.warning("auto_bet: gate EV top-down incoerente su %s "
                           "(ev=%.4f >= %.3f=%s vs quota %.4f >= %.4f) — "
                           "verificare TOP_DOWN_MARGIN/EV_MIN",
                           pick.get("match_id"), ev, ev_min_eff,
                           ev >= ev_min_eff, quota, required)
        return {"ok": True, "ev": round(ev, 6), "p_true": round(p_true, 6),
                "true_odd": round(true_odd, 4),
                "required_price": round(required, 4),
                "ev_min": ev_min_eff, "trigger": bool(ev >= ev_min_eff),
                "overround": probs.get("overround"),
                # Consenso multi-oracolo (26/09): quali fonti hanno formato la
                # p_true, se il validatore ha confermato e se si e' ripiegati
                # su una sola fonte. E' telemetria del gate, non una soglia.
                "oracle_sources": probs.get("sources"),
                "oracle_validated": probs.get("validated"),
                "oracle_fallback": probs.get("fallback")}
    except Exception as e:
        return {"ok": False, "reason": f"errore valutazione ({e})"}


def t60_kill_switch_status() -> dict:
    """Stato del CB2 (kill switch patrimoniale a 30 USDC).

    Lettura FAIL-CLOSED del flag persistente: file illeggibile = blocco
    attivo (meglio fermarsi che dubitare). Il flag e' scritto solo da
    `t60_check_wallet_kill` (o a mano): finche' non c'e', il sistema respira.
    """
    try:
        data = json.loads(T60_KILL_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("flag non e' un dict")
        return {
            "triggered": True,
            "threshold": T60_KILL_WALLET_USDC,
            "triggered_at": data.get("triggered_at"),
            "wallet_equity": data.get("wallet_equity"),
            "reason": data.get("reason"),
            "file": str(T60_KILL_FILE),
        }
    except FileNotFoundError:
        return {"triggered": False, "threshold": T60_KILL_WALLET_USDC,
                "file": str(T60_KILL_FILE)}
    except Exception as e:
        return {"triggered": True, "threshold": T60_KILL_WALLET_USDC,
                "reason": f"flag illeggibile ({e})", "file": str(T60_KILL_FILE)}


def t60_clear_kill() -> None:
    """Rimuove il flag CB2 (riattivazione manuale dopo l'emergenza)."""
    try:
        T60_KILL_FILE.unlink()
    except FileNotFoundError:
        pass


def t60_check_wallet_kill(equity: float | None) -> bool:
    """CB2: True se l'equity wallet e' a/ sotto la soglia (scrive il flag
    persistente SOLO al primo innescio reale).

    La soglia vale sull'EQUITY (liberi + in gioco), non sul disponibile:
    l'escrow non e' una perdita. Un wallet NON LEGGIBILE (None) NON arma il
    flag: un errore API transitorio non deve arrestare il sistema finche'
    un admin non lo sblocca — il chiamante decide come gestire la lettura
    fallita (il giro normale logga e ripiega sulla cassa, il giro T-60 esce
    fail-closed senza armare nulla).
    """
    if equity is None:
        return False
    try:
        equity_float = float(equity)
    except (TypeError, ValueError):
        return False
    if equity_float > T60_KILL_WALLET_USDC + 1e-9:
        return False
    st = t60_kill_switch_status()
    if st.get("triggered"):
        return True                    # gia' armato: nessuna riscrittura
    payload = {
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "wallet_equity": equity_float,
        "threshold": T60_KILL_WALLET_USDC,
        "reason": (f"equity wallet {equity_float:.2f} <= soglia "
                   f"{T60_KILL_WALLET_USDC:.2f} USDC"),
    }
    try:
        T60_KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = T60_KILL_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, T60_KILL_FILE)
    except Exception as e:
        logger.error("auto_bet: scrittura flag T60_KILL fallita: %s", e)
    logger.error("auto_bet: T60 KILL SWITCH — %s", payload["reason"])
    return True


def _t60_emergency_alert(reason: str) -> None:
    """CB2: alert di EMERGENZA via bot Telegram (POST diretto, fail-safe).

    Inviato al primo innescio del kill switch patrimoniale; il promemoria
    persistente (1/giorno) e' cura del job `t60_kill_watch_job` in bot.py.
    Mai un'eccezione: l'alert non puo' fermare l'arresto che annuncia.
    """
    try:
        from decision.gateways import _send_telegram, admin_targets
        targets = admin_targets()
        if targets:
            _send_telegram(
                "🚨 *KILL SWITCH T-60: SISTEMA ARRESTATO*\n\n"
                f"{reason}\n\n"
                "⚠️ Tutti i processi di puntata sono FERMI (CB2, "
                f"soglia {T60_KILL_WALLET_USDC:.2f} USDC).\n"
                "Riattivazione manuale: `/t60reset` dopo il top-up del "
                "wallet.", targets)
    except Exception as e:
        logger.warning("auto_bet: alert CB2 non inviato: %s", e)


def validate_order_payload(payload: dict) -> "tuple[bool, object | None, list[str]]":
    """CB3: valida il payload d'ordine col contratto Pydantic rigido.

    Ritorna `(ok, contract, errors)`. CB1/CB2 sono verificati QUI oltre che
    nel motore: un payload che li viola e' SCARTATO (non cappato in silenzio),
    perche' a valle del contratto non esistono correttivi impliciti. Gli
    errori sono machine-readable per la riga del ledger.
    """
    from decision.models import T60OrderContract, t60_executable
    errors: list[str] = []
    try:
        contract = T60OrderContract.model_validate(payload)
    except Exception as exc:
        return False, None, [str(exc)]
    if T60_ORDER_VALIDATION:
        if not t60_executable(contract.stake, contract.price):
            errors.append(
                f"circuit breaker: stake {contract.stake} > "
                f"{T60_MAX_STAKE_USDC} o quota {contract.price} > {T60_MAX_ODDS}")
    return (not errors), (contract if not errors else None), errors


def t60_dispatch_pending(bankroll: float | None = None) -> list[dict]:
    """Esegue gli ordini T-60: righe `decisions` validate nella finestra T-60.

    Pipeline (fail-closed ad ogni passo, nessun ordine "a senso"):
    1. CB2: kill switch patrimoniale attivo -> nessun ordine;
    2. CB4: gate di mercato (feed SX validato) -> blocca il giro;
    3. per ogni riga `validate/approve` in finestra (T-60..T-50):
       a. dedup: UNIQUE(match_id, esito) su `bets`;
       b. CB3: contratto Pydantic rigido (payload malformato -> riga di
          rifiuto sul ledger `bets` mode='rejected-t60', MAI al provider);
       c. CB1: stake = hard cap 1.00 USDC (mai Kelly, mai di piu');
       d. esecuzione REALE via `_live_fill` (floor EV + liquidita').
    Le righe in `sim` NON partono mai verso il provider (mode='t60-sim':
    paper trading del timing T-60, stesso circuito di validate/dedup).
    """
    from tracker import _get_conn, save_bet, bet_exists_open
    mode = _execution_mode(allow_sim=True)
    if mode == "off":
        return []
    if t60_kill_switch_status().get("triggered"):
        logger.error("auto_bet: T60 giro bloccato dal CB2 (kill switch "
                     "patrimoniale attivo)")
        return []
    # CB4: il giro esecutivo parte SOLO col feed di mercato verificato (la
    # stessa rete di protezione del giro normale: senza dati di mercato
    # freschi, conformi e validati non si punta).
    allowed, gate_reason, identity = _market_feed_gate(
        request_id=f"t60-{datetime.now(timezone.utc):%Y%m%dT%H%M}")
    _last_market_gate.update({
        "blocked": not allowed, "reason": gate_reason.split(":", 1)[0],
        "detail": gate_reason,
        "checked_at": datetime.now(timezone.utc).isoformat(), **identity})
    if not allowed:
        logger.error("auto_bet: T60 giro bloccato dal gate di mercato — %s",
                     gate_reason)
        return []
    # Bankroll = equity wallet reale in LIVE, cassa altrimenti.
    equity = None
    if bankroll is None:
        if mode == "live":
            snap = _live_wallet_snapshot()
            if snap is None:
                logger.error("auto_bet: T60 wallet non leggibile: nessun "
                             "ordine (fail-closed)")
                return []
            equity = snap["equity"]
            if t60_check_wallet_kill(equity):
                return []
            bankroll = equity
        else:
            try:
                from adaptive_staking import bankroll_stats
                bankroll = bankroll_stats().get("current") or 0.0
            except Exception:
                bankroll = 0.0
    stake = t60_stake(bankroll, mode=mode)
    if stake <= 0:
        logger.info("auto_bet: T60 stake sotto il minimo ordine "
                    "(bankroll %.2f): nessun ordine", bankroll or 0.0)
        return []
    now = datetime.now(timezone.utc)
    win_lo = now - timedelta(minutes=T60_WINDOW_MIN_MIN + 10)   # tolleranza dedup
    win_hi = now + timedelta(minutes=T60_WINDOW_MIN_MIN + 10)
    conn = _get_conn()
    rows = conn.execute(
        "SELECT d.signal_id, d.record_id, d.match_id, d.league, m.home_team, "
        "m.away_team, d.outcome, d.selection_label, m.commence_time, d.price, "
        "d.verdict, d.mode, d.provider, d.created_at "
        "FROM decisions d JOIN matches m ON d.match_id = m.id "
        "WHERE d.verdict = 'approve' AND d.status = 'validated' "
        "AND m.commence_time IS NOT NULL AND m.commence_time >= ? "
        "AND m.commence_time <= ? ORDER BY m.commence_time",
        (win_lo.isoformat(), win_hi.isoformat())).fetchall()
    conn.close()
    placed: list[dict] = []
    for (signal_id, record_id, mid, league, home, away, outcome, sel_label,
         kickoff, price, verdict, row_mode, provider, created_at) in rows:
        k = _parse_iso_utc(kickoff)   # SEMPRE aware UTC (contratto CB3)
        if t60_window(k) != "within":
            continue                     # fuori finestra: solo scan/osservazione
        esito = outcome
        canon = _canonical_esito(outcome, home, away)
        if canon:
            esito = canon["esito_key"]
        if bet_exists_open(mid, esito):
            continue                     # CB dedup: UNIQUE(match_id, esito)
        created = _parse_iso_utc(created_at)  # ledger puo' avere ISO naive
        payload = {
            "signal_id": signal_id, "record_id": record_id,
            "match_id": mid, "league": (league or "").strip(),
            "market": "1X2", "outcome": outcome,
            "home": home or "", "away": away or "",
            "price": float(price or 0), "stake": float(stake),
            "verdict": verdict, "mode": mode, "provider": provider or "sxbet",
            # Timestamp con fuso OBBLIGATORIO: un payload con date naive
            # NON supera CB3 (riga di rifiuto, mai ordine).
            "kickoff": k.isoformat() if k else "",
            "created_at": created.isoformat() if created else "",
        }
        ok, contract, errors = validate_order_payload(payload)
        if not ok:
            # CB3: payload malformato o circuit breaker violato -> riga di
            # rifiuto sul ledger, MAI verso il provider.
            logger.error("auto_bet: T60 payload SCARTATO per %s: %s",
                         mid, "; ".join(errors))
            try:
                save_bet(match_id=mid, mercato="1X2", esito=esito,
                         price=float(price or 0), stake=0.0,
                         mode="rejected-t60",
                         status="PAYLOAD_REJECTED: " + " | ".join(errors))
            except Exception as e:
                logger.warning("auto_bet: riga rifiuto T60 %s: %s", mid, e)
            continue
        if mode != "live":
            placed.append({"match_id": mid, "home": home, "away": away,
                           "esito_key": esito, "price": float(price),
                           "stake": 0.0, "mode": "t60-sim",
                           "status": "SIMULATED_T60"})
            continue
        filled = _live_fill({"match_id": mid, "home": home, "away": away,
                             "esito_key": esito, "esito_raw": outcome,
                             "mercato": "1X2", "commence": kickoff,
                             "quota": float(price), "league": league or "",
                             "best_ev": 0.0},
                            stake, float(price))
        if filled is None:
            continue
        if not filled.get("ok"):
            logger.warning("auto_bet: T60 ordine non piazzato per %s: %s",
                           mid, filled.get("error") or filled.get("status"))
            continue
        # Stessa barriera della corsia normale (26/09): senza un bet_id emesso
        # dall'exchange la riga live NON si scrive. `_live_fill` gia' fallisce
        # chiuso, ma questo e' il SECONDO punto di scrittura: la guardia va
        # ripetuta qui, non ereditata per fiducia.
        if not filled.get("bet_id"):
            logger.error("auto_bet: T60 ordine per %s senza bet_id: riga "
                         "live NON scritta sul ledger", mid)
            continue
        record = {"match_id": mid, "home": home, "away": away,
                  "esito_key": esito, "price": float(filled["price"] or price),
                  "stake": float(filled["stake"] or stake),
                  "mode": "t60-live",
                  "status": filled.get("status") or "FILLED_UNCONFIRMED",
                  "bet_id": filled.get("bet_id")}
        placed.append(record)
        try:
            save_bet(match_id=mid, mercato="1X2", esito=esito,
                     market_id=filled["market_id"],
                     selection_id=filled["selection_id"],
                     price=record["price"], stake=record["stake"],
                     mode="live", status=record["status"],
                     bet_id=filled.get("bet_id"))
        except Exception as e:
            logger.warning("auto_bet: salvataggio T60 live %s: %s", mid, e)
    if placed:
        logger.info("auto_bet: T60 %d ordini (%s)", len(placed), mode)
    return placed

# --- FILTRO LIQUIDITA' SX (tarato 11/09/2026) ------------------------------
# SX Bet e' un EXCHANGE: la quota mostrata esiste solo se c'e' chi compra
# dall'altra parte. Un book sottile produce slippage o un riempimento
# parziale, quindi prima di ogni ordine REALE si pretende che la profondita'
# BACK disponibile AL FLOOR (quota-segnale o meglio) copra lo stake con
# margine:
#
#     richiesto = max(stake * SX_DEPTH_MULTIPLIER, SX_MIN_EXEC_DEPTH_USDC)
#
#   SX_DEPTH_MULTIPLIER (default 1.6) -> lo stake non deve esaurire il book:
#     se lo stake consuma meta' del lato, il resto della size muove il prezzo
#     e la quota "richiesta" non e' piu' garantita.
#   SX_MIN_EXEC_DEPTH_USDC (default 20.0) -> soglia ASSOLUTA di libro al
#     floor per qualunque stake: un mercato quasi vuoto non e' negoziabile
#     (e' la stessa soglia che `sx_signals` pretende sulla leg giocata, cosi'
#     non si generano segnali che l'ordine scarterebbe sempre).
# TARATURA 21/09/2026: 2.0/25.0 -> 1.6/20.0 (-20% sul libro richiesto),
# direttiva "volume": allarga la fascia dei book eseguibili senza scendere
# sotto la taglia minima d'ordine. Le guardie restano tutte attive
# (multiplo + soglia assoluta + inv_sum + fascia quota).
# Con STAKE_CAP_HARD attivo l'ordine viene SALTATO (fail-closed) e lo scarto
# finisce nel monitor (liquidity_monitor, kind="order").
SX_DEPTH_MULTIPLIER = float(os.getenv("SX_DEPTH_MULTIPLIER", "1.6"))
MIN_EXEC_DEPTH_USDC = float(os.getenv("SX_MIN_EXEC_DEPTH_USDC", "20.0"))


def required_depth(stake: float) -> float:
    """Profondita' BACK minima al floor per eseguire `stake` senza slippage.

    E' il vincolo che il guardrail d'ordine applica: il massimo tra il
    multiplo dello stake (margine sul book) e la soglia assoluta di libro.
    """
    try:
        return max(float(stake) * SX_DEPTH_MULTIPLIER, MIN_EXEC_DEPTH_USDC)
    except (TypeError, ValueError):
        return MIN_EXEC_DEPTH_USDC

# --- Flat-stake override (09/09) ---
# In alternativa al Kelly dinamico si puo' piazzare un importo FISSO per
# ogni segnale value/strong_value (Calcio 1X2): env
# AUTO_BET_STAKE_MODE=flat (default 'adaptive' = Kelly dinamico 08/09),
# importo AUTO_BET_FLAT_STAKE_EUR (default 1.0 = minimo ordine SX Bet in
# USDC). I cap di sicurezza restano SEMPRE attivi ma vengono applicati a
# UNITÀ INTERE (vedi apply_flat_budget): con un saldo wallet ~12 USDC
# entrano al massimo 3-4 ordini da 1 USDC al giorno (cap correlazione 30%
# + esposizione totale 40%), mai frazioni non piazzabili.
STAKE_MODE = os.getenv("AUTO_BET_STAKE_MODE", "adaptive").strip().lower()
FLAT_STAKE_EUR = float(os.getenv("AUTO_BET_FLAT_STAKE_EUR", "1.0"))


def cap_hard_active() -> bool:
    """True se il cap per bet e' vincolante (mai superato dal floor)."""
    return STAKE_CAP_HARD


def hard_cap_skip_message(stake: float, bankroll: float) -> str:
    """Motivo standard dello skip quando il cap severo blocca l'ordine."""
    return ("CAP SEVERO: stake cappato %.2f USDC < minimo ordine %.2f USDC "
            "(bankroll %.2f) — ordine saltato (fail-closed). Alza il wallet "
            "o imposta STAKE_CAP_HARD=0 per accettare il floor."
            % (stake, MIN_STAKE_EUR, bankroll))


def normalize_stake(stake: float) -> float:
    """Arrotonda allo step configurato (default 0.01) e forza il minimo
    (default 1.0 = minimo ordine SX Bet in USDC, env MIN_STAKE_EUR).
    Sotto il minimo: 0 (no bet)."""
    if stake <= 0:
        return 0.0
    stepped = round(stake / STAKE_STEP_EUR) * STAKE_STEP_EUR
    stepped = round(stepped, 2)
    if stepped < MIN_STAKE_EUR:
        return 0.0
    return stepped


def _kickoff_utc(commence: str | None):
    """Timestamp kickoff come datetime UTC naive (None se non parsabile)."""
    if not commence:
        return None
    try:
        return datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
    except Exception:
        return None


def _correlation_blocks(candidates: list[dict],
                        window_min: int = CORRELATION_WINDOW_MIN) -> list[list[dict]]:
    """Raggruppa i candidati in blocchi CORRELATI per i risk cap.

    Stessa lega con kickoff nella stessa finestra temporale (o stesso
    match_id, sempre correlati: es. 1X2 + Over sulla stessa partita).
    Algoritmo greedy: ogni candidato entra nel primo blocco compatibile
    per tempo (o match_id). Ritorna i blocchi in ordine di comparsa.
    """
    def _key(cand) -> str:
        # Gruppo per LEGA (default 'global' se assente): i match diversi
        # della stessa lega condividono la varianza di giornata/arbitri e
        # si separano in blocchi temporali sulla finestra window_min.
        return f"league:{cand.get('league') or 'global'}"

    def _same_match(a, b) -> bool:
        # Stesso match_id = SEMPRE correlati (es. 1X2 + Over stessa partita),
        # anche se i commence non sono parsabili in modo identico.
        mid = a.get("match_id")
        return bool(mid) and mid == b.get("match_id")

    groups: list[list[dict]] = []
    group_bounds: list[tuple] = []  # (min_kickoff, max_kickoff) UTC naive
    for cand in candidates:
        k = _kickoff_utc(cand.get("commence"))
        key = _key(cand)
        assigned = False
        for gi, g in enumerate(groups):
            if _key(g[0]) != key:
                continue
            gmin, gmax = group_bounds[gi]
            in_window = (k is not None and gmin is not None
                         and abs((k - gmin).total_seconds()) <= window_min * 60)
            if in_window or _same_match(g[0], cand) or (k is None and gmin is None):
                g.append(cand)
                if k is not None:
                    nmin = min(gmin, k) if gmin else k
                    nmax = max(gmax, k) if gmax else k
                    group_bounds[gi] = (nmin, nmax)
                assigned = True
                break
        if not assigned:
            groups.append([cand])
            group_bounds.append((k, k))
    return groups


def apply_correlation_cap(candidates: list[dict], bankroll: float,
                          cap_pct: float = CORRELATION_CAP_PCT,
                          window_min: int = CORRELATION_WINDOW_MIN) -> list[dict]:
    """Riduce gli stake dei candidati correlati per proteggere il bankroll.

    Kelly calcola ogni stake come se le puntate fossero indipendenti: se il
    job piazza piu' esiti correlati (stessa partita, oppure stessa lega con
    kickoff nello stesso blocco temporale), la varianza reale del portafoglio
    e' piu' alta di quella modellata e il rischio di drawdown cresce. Questa
    funzione raggruppa i candidati per match / lega+finestra e, se
    l'esposizione totale del blocco supera cap_pct * bankroll, scala
    PROPORZIONALMENTE gli stake (mantiene il ranking EV, non taglia esiti).

    Regole di correlazione:
    - stesso match_id (es. 1X2 + Over sulla stessa partita): SEMPRE correlati;
    - stessa lega E kickoff entro window_min (stesso blocco temporale): il
      mercato si muove sugli stessi fattori -> varianza condivisa;
    - leghe diverse o kickoff lontani: indipendenti, nessun cap.

    Args:
        candidates: picks con almeno match_id, league (opz.), commence e stake.
        bankroll: bankroll corrente per il cap.
        cap_pct: frazione di bankroll massima per blocco correlato.
        window_min: finestra temporale (minuti) per raggruppare i kickoff.

    Returns:
        I candidati con stake scalati; aggiunge "corr_cap" (True se
        ridotto) e "corr_group" (descrizione del blocco) per il log.
    """
    if len(candidates) < 2 or bankroll <= 0:
        return candidates

    cap = bankroll * cap_pct
    capped_total = 0.0
    capped_groups = 0
    for g in _correlation_blocks(candidates, window_min):
        total = sum(float(c.get("stake", 0) or 0) for c in g)
        if total <= cap:
            continue
        factor = cap / total
        for c in g:
            raw = float(c.get("stake", 0) or 0)
            c["stake"] = round(raw * factor, 2)
            c["corr_cap"] = True
            c["corr_group"] = (f"{len(g)} esiti correlati "
                               f"(esposizione €{total:.2f} > cap €{cap:.2f})")
        capped_total += total - cap
        capped_groups += 1

    if capped_groups:
        logger.info("auto_bet: correlation cap attivo — ridotti €%.2f di "
                    "stake correlati in %d blocchi sopra il cap",
                    capped_total, capped_groups)
    return candidates


def apply_flat_budget(candidates: list[dict], bankroll: float,
                      unit: float | None = None,
                      cap_pct: float = CORRELATION_CAP_PCT,
                      total_cap_pct: float = TOTAL_EXPOSURE_CAP_PCT,
                      already_placed: float = 0.0,
                      window_min: int = CORRELATION_WINDOW_MIN) -> list[dict]:
    """Risk cap a UNITA' INTERE per lo stake FLAT (09/09, Calcio 1X2).

    Con stake fissi da FLAT_STAKE_EUR (= minimo ordine SX Bet, 1 USDC) lo
    scaling proporzionale dei cap generici produrrebbe frazioni NON
    piazzabili: rialzarle al floor sforerebbe i cap. Qui i due cap vengono
    applicati a unita' intere, in ordine di EV decrescente:
      - blocco correlato (stessa lega + finestra 90', o stesso match):
        al massimo floor(30% bankroll / unit) segni;
      - esposizione totale del giorno: al massimo
        floor((40% bankroll - gia' piazzato) / unit) segni complessivi.
    Gli esuberi (in coda per EV) vengono azzerati ("stake"=0) con i flag
    corr_cap/total_cap per il log. Con un saldo wallet ~12 USDC entrano al
    massimo 3-4 ordini da 1 USDC al giorno.
    """
    if not candidates or bankroll <= 0:
        return candidates
    unit = float(unit if unit is not None else FLAT_STAKE_EUR)
    if unit <= 0:
        return candidates

    ordered = sorted(candidates,
                     key=lambda c: float(c.get("best_ev", 0.0) or 0.0),
                     reverse=True)
    # 1) correlation cap: unita' intere per blocco correlato
    max_block = int((bankroll * cap_pct) // unit)
    eligible: list[dict] = []
    for block in _correlation_blocks(ordered, window_min):
        keep = block[:max_block] if max_block > 0 else []
        for c in block:
            if c in keep:
                c["stake"] = unit
                eligible.append(c)
            else:
                c["stake"] = 0.0
                c["corr_cap"] = True
                c["corr_group"] = (
                    f"blocco correlato oltre il cap "
                    f"({len(keep)}x €{unit:.2f} <= €{bankroll * cap_pct:.2f})")
    # 2) cap esposizione totale: unita' intere sul budget residuo del giorno
    budget = max(0.0, bankroll * total_cap_pct - float(already_placed or 0.0))
    max_total = int(budget // unit) if budget > 0 else 0
    eligible.sort(key=lambda c: float(c.get("best_ev", 0.0) or 0.0),
                  reverse=True)
    keep = eligible[:max_total] if max_total > 0 else []
    for c in eligible:
        if c in keep:
            c["stake"] = unit
        else:
            c["stake"] = 0.0
            c["total_cap"] = True
            c["total_cap_group"] = (
                f"esposizione totale del giorno piena: "
                f"{(bankroll * total_cap_pct - float(already_placed or 0.0)):.2f}"
                f" disponibili / €{unit:.2f} a segno")
    logger.info("auto_bet: flat budget attivo — %d segni da €%.2f "
                "(cap correlazione %d/blocco, esposizione %d/giorno)",
                len(keep), unit, max_block, max_total)
    return candidates


def apply_total_exposure_cap(candidates: list[dict], bankroll: float,
                             cap_pct: float = TOTAL_EXPOSURE_CAP_PCT,
                             already_placed: float = 0.0) -> list[dict]:
    """Cap di portafoglio: esposizione totale del giorno <= cap_pct del bankroll.

    Kelly dimensiona ogni stake come se fosse l'unica puntata: anche senza
    correlazione tra i segnali, la varianza del portafoglio cresce con il
    numero di pick. Se la somma di TUTTI gli stake supera cap_pct * bankroll,
    gli stake vengono scalati PROPORZIONALMENTE (mantiene il ranking EV e i
    rapporti tra le puntate, non taglia esiti).

    Con piu' giri al giorno (auto-bet 24/7 dal 08/09) il cap e' RIMANENTE:
    sottrae l'esposizione GIÀ piazzata nei giri precedenti (puntate aperte
    nelle ultime 24h), cosi' il tetto del 40% vale sul giorno intero e non
    per ogni singolo giro.

    Args:
        candidates: picks con stake (gia' passati dal correlation cap).
        bankroll: bankroll corrente.
        cap_pct: frazione di bankroll massima per l'esposizione totale.
        already_placed: stake gia' impegnato in puntate aperte (stesso
            giorno). Default 0 (comportamento storico, un giro solo).

    Returns:
        I candidati con stake scalati (0.0 se il budget e' gia' esaurito);
        aggiunge "total_cap" (True se ridotto) e "total_cap_group" per il log.
    """
    if len(candidates) < 2 or bankroll <= 0:
        return candidates
    total = sum(float(c.get("stake", 0) or 0) for c in candidates)
    cap = bankroll * cap_pct - float(already_placed or 0.0)
    if cap <= 0:
        # Budget del giorno gia' esaurito dai giri precedenti: nessun nuovo
        # ordine (i candidati vengono azzerati e filtrati dal chiamante).
        for c in candidates:
            c["stake"] = 0.0
            c["total_cap"] = True
            c["total_cap_group"] = (f"esposizione già piazzata "
                                     f"€{float(already_placed):.2f} >= cap "
                                     f"€{bankroll * cap_pct:.2f}")
        logger.info("auto_bet: cap esposizione totale esaurito — "
                    "€%.2f già piazzati (cap €%.2f): nessun nuovo ordine",
                    float(already_placed), bankroll * cap_pct)
        return candidates
    if total <= cap:
        return candidates
    factor = cap / total
    for c in candidates:
        raw = float(c.get("stake", 0) or 0)
        c["stake"] = round(raw * factor, 2)
        c["total_cap"] = True
        c["total_cap_group"] = (f"esposizione totale €{total:.2f} > cap "
                                 f"residuo €{cap:.2f} (già piazzati "
                                 f"€{float(already_placed):.2f})")
    logger.info("auto_bet: cap esposizione totale attivo — ridotti €%.2f di "
                "stake (%d pick sopra il cap residuo €%.2f)",
                total - cap, len(candidates), cap)
    return candidates


def _today_placed_stake(hours: float = 24.0) -> float:
    """Stake GIÀ piazzato nelle ultime `hours` ore (puntate ancora aperte).

    Con l'auto-bet 24/7 (piu' giri al giorno) il cap di esposizione totale
    deve contare anche le puntate dei giri precedenti: il budget giornaliero
    e' condiviso tra i giri, non per-giro.
    """
    try:
        from tracker import _get_conn
        conn = _get_conn()
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM bets "
            "WHERE esito_finale IS NULL AND created_at >= ?",
            (cutoff,)).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] else 0.0
    except Exception as e:
        logger.warning("auto_bet: lettura esposizione gia' piazzata "
                       "fallita: %s", e)
        return 0.0


# --- RECINTO DI CAPITALE (direttiva 27/09/2026): le tre guardie condivise da
# TUTTE le corsie di ordine (quella storica e la catena piramidale). Sono
# funzioni in un posto solo perche' due copie di una soglia di denaro
# divergono, e la prima che diverge e' quella che spende.

def _open_live_snapshot() -> tuple[float, int]:
    """(stake in gioco, numero di ordini aperti) — il capitale IMMOBILIZZATO.

    Solo `mode='live'`: la cassa simulata non immobilizza capitale e non deve
    occupare il recinto. Un'eccezione di lettura restituisce `inf`: meglio
    dichiarare un'esposizione ignota che aprire il varco del recinto (stessa
    direzione fail-closed del CB2).

    Il RILASCIO e' dinamico per costruzione: la lettura non filtra per data
    ma per `esito_finale IS NULL`, cioe' per gli ordini ANCORA IN CORSO —
    appena un settlement chiude una riga l'esposizione scende da sola, senza
    finestre giornaliere da riarmare (direttiva 28/09/2026).
    """
    try:
        from tracker import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(stake), 0), COUNT(*) FROM bets "
            "WHERE esito_finale IS NULL AND mode = 'live'").fetchone()
        conn.close()
        stake = float(row[0]) if row and row[0] is not None else 0.0
        count = int(row[1]) if row and row[1] is not None else 0
        return stake, count
    except Exception as e:
        logger.warning("auto_bet: lettura esposizione aperta fallita: %s", e)
        return float("inf"), -1


def _open_live_exposure() -> float:
    """Stake REALE attualmente in gioco (puntate live non ancora saldate)."""
    return _open_live_snapshot()[0]


def open_exposure_status(bankroll: float) -> dict:
    """Stato del recinto d'esposizione aperta (lettura, nessuna scrittura).

    Ritorna {open_stake, count, cap, blocked, pct, reason}: `blocked=True`
    quando l'esposizione aperta raggiunge la soglia — da quel momento il giro
    non piazza ordini reali ma continua a valutare e telemetrizzare (shadow).
    `cap` e' SEMPRE `bankroll x 40%` letto fresco: con il capitale aggiornato
    il tetto segue (compounding automatico, nessun valore congelato).
    """
    bankroll = float(bankroll or 0.0)
    cap = max(bankroll, 0.0) * OPEN_EXPOSURE_CAP_PCT
    open_stake, count = _open_live_snapshot()
    blocked = open_stake >= cap
    return {
        "open_stake": open_stake,
        "count": count,
        "cap": round(cap, 2),
        "bankroll": round(bankroll, 2),
        "pct": (round(open_stake / bankroll, 4) if bankroll > 0 else None),
        "blocked": bool(blocked),
        "reason": ("esposizione aperta %.2f >= tetto %.2f (%.0f%% del bankroll "
                   "%.2f): ordini reali sospesi, sola shadow finche' i "
                   "settlement non liberano fondi"
                   % (open_stake, cap, OPEN_EXPOSURE_CAP_PCT * 100, bankroll))
        if blocked else "",
    }


def exposure_allows(bankroll: float, new_stake: float) -> dict:
    """Il recinto accetta un NUOVO ordine di `new_stake` USDC?

    Direttiva 28/09/2026: il 40% e' un tetto sul capitale immobilizzato
    SIMULTANEO, non solo una soglia di blocco: un nuovo ordine e' ammesso solo
    se l'esposizione PROIETTATA (aperta + nuovo stake) resta entro il cap.
    Con equity 33.55 USDC (cap 13.42) e stake fisso 1.50 entrano 8 ordini
    (12.00 USDC); il nono porterebbe a 13.50 > 13.42 e viene respinto.

    E' la lettura che l'Advisor interroga a OGNI ciclo (via Execution Agent):
    un solo punto di verita' per il tetto, cosi' la corsia di denaro e il
    consigliere non possono divergere.

    Fail-closed: una lettura impossibile (`open_stake` non finito) o un
    bankroll non positivo non autorizzano l'ordine.
    """
    state = open_exposure_status(bankroll)
    stake = max(float(new_stake or 0.0), 0.0)
    open_stake = float(state["open_stake"])
    cap = float(state["cap"])
    usable = bool(bankroll) and bankroll > 0 and cap > 0 \
        and open_stake == open_stake and open_stake != float("inf")
    projected = round(open_stake + stake, 2) if usable else float("inf")
    allowed = bool(usable and projected <= cap)
    out = dict(state)
    out.update({
        "new_stake": round(stake, 2),
        "projected": projected,
        "allowed": allowed,
        "reason": "" if allowed else _exposure_deny_reason(
            usable, projected, cap, bankroll, open_stake,
            int(state.get("count") or 0), stake),
    })
    return out


def _exposure_deny_reason(usable: bool, projected: float, cap: float,
                          bankroll: float, open_stake: float, count: int,
                          stake: float) -> str:
    """Motivo (leggibile) del rifiuto del recinto — sempre dichiarato."""
    if usable:
        return ("esposizione proiettata %.2f > tetto %.2f (%.0f%% del bankroll "
                "%.2f): %d ordini aperti per %.2f USDC + %.2f nuovi — ordine "
                "respinto, il tetto si libera da solo quando i settlement "
                "chiudono un match"
                % (projected, cap, OPEN_EXPOSURE_CAP_PCT * 100, float(bankroll or 0.0),
                   count, open_stake, stake))
    if float(bankroll or 0.0) <= 0:
        return ("bankroll non positivo (%.2f): nessun nuovo ordine reale"
                % float(bankroll or 0.0))
    return ("recinto di esposizione non leggibile (esposizione %.2f): nessun "
            "nuovo ordine (fail-closed)" % open_stake)


def cap_order_stake(stake: float) -> float:
    """Tetto assoluto per singolo ordine (micro-stake, 27/09/2026).

    Riduce, non alza mai: un cap non puo' creare un ordine. Il tetto di
    1.50 USDC e' una regola di business inviolabile: non puo' essere
    disattivato neanche impostando l'ENV a 0.0. Se il tetto scende sotto
    il minimo ordine dell'exchange la funzione restituisce comunque il tetto
    — il chiamante (fail-closed) salta la puntata invece di alzarla al floor.
    """
    stake = float(stake or 0.0)
    return min(stake, 1.50)


# --- STAKE FISSO per gli ordini reali (direttiva 28/09/2026) ----------------

def fixed_order_stake() -> float:
    """Importo FISSO di ogni ordine reale (default 1.50 USDC), 0 = disattivo.

    Un solo punto di verita': l'env puo' solo ABBASSARE l'importo (il tetto
    per singolo ordine resta inviolabile), mai alzarlo. Un valore non
    numerico ricade sul default di progetto invece di creare uno stake
    casuale.
    """
    try:
        fixed = float(FIXED_STAKE_USDC)
    except (TypeError, ValueError):
        fixed = 1.50
    if fixed <= 0:
        return 0.0
    return cap_order_stake(round(fixed, 2))


def fixed_stake_active() -> bool:
    """True se la size degli ordini reali e' l'importo fisso (default)."""
    return fixed_order_stake() > 0


def order_stake(stake: float, spendable: float = float("inf")) -> float:
    """Stake FINALE di un ordine REALE — unico punto di verita'.

    Direttiva 28/09/2026: importo FISSO (`ORDER_FIXED_STAKE_USDC`, 1.50 USDC)
    invece del dimensionamento dinamico. Con l'importo fisso disattivato
    (0) resta il percorso storico (tetto per-ordine sopra lo stake calcolato).

    Il vincolo di CASSA non e' negoziabile: se i fondi liberi del wallet non
    coprono l'importo fisso la funzione restituisce 0 e il chiamante salta
    l'ordine (fail-closed) — mai un importo diverso da quello della direttiva
    per far passare comunque un ordine.
    """
    try:
        cassa = float(spendable)
    except (TypeError, ValueError):
        cassa = float("inf")
    fixed = fixed_order_stake()
    if fixed > 0:
        # Importo fisso: se i fondi liberi non lo coprono l'ordine e' 0
        # (fail-closed), mai un importo diverso dalla direttiva.
        return fixed if cassa >= fixed else 0.0
    # Staking dinamico storico: tetto per-ordine SOPRA il vincolo di cassa
    # (i fondi in escrow non si possono spendere due volte).
    return min(cap_order_stake(stake), cassa)


def chief_execution_enabled() -> bool:
    """La catena piramidale puo' emettere ordini REALI in questo giro?

    Lettura a ogni giro (non a import): cambiare la variabile su Railway si
    applica al giro successivo senza redeploy. Default "off" = nessun ordine.
    """
    return ((os.getenv("CHIEF_EXECUTION", "off") or "off").strip().lower()
            == "live")


def _norm_team(name: str) -> str:
    from tracker import _norm_team as nt
    return nt(name)


_TEAM_ALIAS_CACHE: dict[str, str] = {}


def _resolve_team(name: str) -> str:
    """Normalizza un nome squadra E risolve gli alias comuni (TEAM_MAP).

    'AC Milan' -> 'milan', 'West Ham United' -> 'west ham': allinea i nomi
    del segnale (the-odds-api) con quelli canonici. Fallback sicuro: se
    TEAM_MAP non e' importabile, resta solo la normalizzazione base.
    """
    base = _norm_team(name)
    if base in _TEAM_ALIAS_CACHE:
        return _TEAM_ALIAS_CACHE[base]
    resolved = base
    try:
        from fixture_engine import TEAM_MAP
        resolved = _norm_team(TEAM_MAP.get(base, base))
    except Exception:
        pass
    _TEAM_ALIAS_CACHE[base] = resolved
    return resolved


_DRAW_NAMES = ("the draw", "draw", "pareggio")


def _canonical_esito(esito: str, home: str, away: str) -> dict | None:
    """Esito del segnale -> (mercato, esito_key) canonici per il ledger.

    Sistema 1X2 SOLO (calcio) + 2-way (tennis). OU2.5 escluso definitivamente
    dal 06/09: il mercato non genera candidati.
    """
    el = str(esito or "").lower().strip()
    if el in ("x", "draw", "pareggio"):
        return {"mercato": "1X2", "esito_key": "X"}
    if el == "1":
        return {"mercato": "1X2", "esito_key": "1"}
    if el == "2":
        return {"mercato": "1X2", "esito_key": "2"}
    hn, an, en = _resolve_team(home), _resolve_team(away), _resolve_team(el)
    if en == hn:
        return {"mercato": "1X2", "esito_key": "1"}
    if en == an:
        return {"mercato": "1X2", "esito_key": "2"}
    return None


def _today_value_picks() -> list[dict]:
    """Partite in programma nelle prossime 24h con segnale value/strong_value
    (esito canonico).

    Fonte: ledger `predictions` (status per OGNI esito), NON `match_analysis`
    che registra solo il best-per-EV: se il best e' rejected ma un altro
    esito dello stesso match e' value (es. Derby: best=Draw rejected,
    Derby/West Brom value) il match spariva dai candidati e il bot non
    piazzava nulla (bug 09/09). Un pick per match: il candidato value con
    EV piu' alto (comportamento storico di match_analysis.best).

    Finestra MOBILE (now .. now+24h) invece del giorno calendario: a fine
    giornata UTC un match con kickoff poco dopo la mezzanotte cadrebbe nel
    giorno dopo e verrebbe perso dal filtro per data. Include market_edge,
    market_prob, best_ev e status per l'adaptive staking.

    STRATEGIA SOLO FAVORITI (11/09): il filtro quota/prob. di mercato e'
    ripetuto QUI (difesa in profondita') oltre che nel motore, cosi' eventuali
    righe storiche di segnali su sfavorite/quote alte — o scritte da moduli
    non aggiornati — non possono mai trasformarsi in un ordine.

    STRATEGIA SOLO CAMPIONATI VINCENTI (12/09, applicata qui dal 15/09):
    stessa difesa in profondita' sulla LEGA (`value_filter.league_allowed`),
    fail-closed se la lega manca. Il ledger resta completo (telemetria),
    il gate ferma solo gli ordini.
    """
    from tracker import _get_conn
    from value_filter import (ODDS_MIN, ODDS_MAX, MIN_FAVOURITE_MARKET_PROB,
                              FAVOURITES_ONLY, league_allowed)
    conn = _get_conn()
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    start = now_utc.isoformat().replace("+00:00", "Z")
    end = (now_utc + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    rows = c.execute('''SELECT m.id, m.home_team, m.away_team, m.commence_time,
                               m.league, p.esito, p.quota, p.market_edge,
                               p.market_prob, p.ev, p.status
                        FROM matches m JOIN predictions p ON m.id = p.match_id
                        WHERE m.commence_time >= ? AND m.commence_time < ?
                          AND p.status IN ('value','strong_value','moderate')
                          AND p.mercato = '1X2'
                          AND p.esito_finale IS NULL
                        ORDER BY p.ev DESC''', (start, end)).fetchall()
    conn.close()
    seen: set[str] = set()
    out = []
    for (mid, home, away, commence, league, esito, quota,
         m_edge, m_prob, ev, status) in rows:
        if FAVOURITES_ONLY:
            if quota is None or float(quota) > ODDS_MAX:
                logger.info("auto_bet: skip %s %s @ %s (quota > %.2f: "
                            "strategia solo favoriti)", mid, esito, quota,
                            ODDS_MAX)
                continue
            if float(quota) < ODDS_MIN:
                logger.info("auto_bet: skip %s %s @ %s (quota < %.2f: "
                            "fascia favoriti %.2f-%.2f)", mid, esito, quota,
                            ODDS_MIN, ODDS_MIN, ODDS_MAX)
                continue
            if m_prob is not None and float(m_prob) < MIN_FAVOURITE_MARKET_PROB:
                logger.info("auto_bet: skip %s %s (prob. mercato %.2f < %.2f)",
                            mid, esito, float(m_prob),
                            MIN_FAVOURITE_MARKET_PROB)
                continue
        # STRATEGIA SOLO CAMPIONATI VINCENTI (12/09): gate di lega ripetuto
        # QUI (difesa in profondita'). Fino al 15/09 questa corsia era CIECA:
        # i candidati non portavano la lega, `is_sane(league="")` ammetteva
        # tutto e il bot ha puntato leghe vietate (EFL Cup, Scottish, La
        # Liga: 0 vinte su 4 chiuse). La lega arriva da `matches.league`
        # (mappata dal resolver, mai indovinata).
        # FAIL-CLOSED sulla lega assente: sul volume NESSUNA riga con partita
        # in `matches` ha lega vuota (le vuote sono orfane senza riga, quindi
        # senza mercato): se manca, non si sa cosa si sta giocando -> skip.
        league_name = (league or "").strip()
        if not league_name:
            logger.info("auto_bet: skip %s %s (lega assente: gate "
                        "STRATEGY_LEAGUES fail-closed)", mid, esito)
            continue
        if not league_allowed(league_name):
            logger.info("auto_bet: skip %s %s (lega '%s' fuori dai "
                        "campionati vincenti)", mid, esito, league_name)
            continue
        if mid in seen:
            continue  # un pick per match (best EV tra i value)
        seen.add(mid)
        canon = _canonical_esito(esito, home, away)
        if not canon:
            continue
        out.append({"match_id": mid, "home": home, "away": away,
                    "commence": commence, "league": league or "",
                    "esito_raw": esito,
                    "quota": float(quota or 0),
                    "market_edge": float(m_edge) if m_edge is not None else None,
                    "market_prob": float(m_prob) if m_prob is not None else None,
                    "best_ev": float(ev) if ev is not None else 0.0,
                    "status": status or "value",
                    **canon})
    return out


def _multi_market_picks() -> list[dict]:
    """Pick OU/AH dal ledger multi-mercato (solo i mercati con ordini ACCESI).

    Corsia definita in `multi_market.live_picks`: **AH live** (ordini reali),
    **OU shadow** (telemetria: ENABLE_LIVE_OU=0 di default). Gate di lega,
    fascia quota, lato favorito e riconoscibilita' del mercato a linea sono
    ripetuti la' dentro (difesa in profondita'), quindi qui non si riapplica
    nulla: un'unica implementazione, un solo posto da controllare.

    Fail-safe: qualunque errore ritorna [] — la corsia multi-mercato non deve
    poter fermare il giro 1X2 in produzione.
    """
    try:
        import multi_market
        picks = multi_market.live_picks()
    except Exception as e:
        logger.warning("auto_bet: corsia multi-mercato non disponibile (%s)", e)
        return []
    if picks:
        logger.info("auto_bet: %d pick multi-mercato dalle corsie live (%s)",
                    len(picks), ", ".join(multi_market.live_markets())
                    or "nessuna")
    return picks


def _esports_picks() -> list[dict]:
    """Corsia eSports: candidati +EV dall'oracolo OddsPapi (30/09/2026).

    Gli eSports non sono visibili al percorso calcio (la discovery di
    `sx_signals`/`multi_market` e' `sportIds=5`) e **the-odds-api non li ha**:
    senza un oracolo esterno il gate top-down risponderebbe `no_oracle` su ogni
    riga. La corsia `esports_lane` fa discovery SX (sport 9, type 52), aggancia
    la fixture OddsPapi e calcola l'EV contro le probabilita' fair de-vigate di
    Pinnacle — con la STESSA soglia del calcio (`value_filter.EV_MIN`, importata).

    E' SOLO una fonte di candidati: stake fisso, recinto 40%/30%, T-60,
    liquidita', dedup e gate di mercato restano quelli del giro, applicati a
    valle. Nessuna scrittura di ledger qui (e' `run_today_bets` che salva).

    Fail-safe: qualunque errore (o corsia spenta) ritorna [] — una classe di
    rischio nuova non deve poter fermare le puntate di calcio.
    """
    try:
        import esports_lane
        picks = esports_lane.picks()
    except Exception as e:
        logger.warning("auto_bet: corsia eSports non disponibile (%s)", e)
        return []
    if picks:
        logger.info("auto_bet: %d pick eSports dall'oracolo OddsPapi", len(picks))
    return picks


def _tennis_picks() -> list[dict]:
    """Corsia TENNIS: candidati +EV dall'oracolo Pinnacle a 2 esiti (30/09/2026).

    Direttiva "Sblocco Totale LIVE": il tennis e' in **Denaro Reale**, senza
    flag di simulazione. La corsia (`tennis_lane`) fa discovery SX (sport 6,
    type 52: un mercato per match, lati sulle chiavi 1/2), aggancia l'oracolo
    Pinnacle a 2 esiti dalle cache gia' scaricate (0 crediti) e calcola l'EV
    contro la probabilita' fair de-vigata, con la soglia PROPRIA del tennis
    (`tennis_lane.EV_MIN`, default 2.5% -> env `TENNIS_EV_MIN`).

    E' SOLO una fonte di candidati: stake fisso, recinto 40%/30%, T-60,
    liquidita', dedup e gate di mercato restano quelli del giro, applicati a
    valle. Il pick porta `market_id`/`selection_id` di SX (l'ordine non deve
    ri-risolvere il mercato: la struttura a UN mercato per match non e' quella
    del moneyline eSports, fatta di un mercato per SQUADRA).

    Fail-safe: qualunque errore (o corsia spenta) ritorna [] — una classe di
    rischio nuova non deve poter fermare le puntate di calcio/eSports.
    """
    try:
        import tennis_lane
        picks = tennis_lane.picks()
    except Exception as e:
        logger.warning("auto_bet: corsia tennis non disponibile (%s)", e)
        return []
    if picks:
        logger.info("auto_bet: %d pick tennis dall'oracolo Pinnacle a 2 esiti",
                    len(picks))
    return picks


def _top_down_picks() -> list[dict]:
    """Corsia TOP-DOWN LIVE: candidati 1X2 senza il filtro bottom-up.

    Direttiva 25/09/2026 ("bypass del filtro quote e merge"): sul denaro REALE
    il giudice del prezzo non e' piu' la fascia favoriti 1.30-1.80 ne' il tier
    del modello, ma l'ORACOLO Pinnacle de-vigato (`_top_down_eval`, ev >=
    EV_MIN, fail-closed senza oracolo). Questa corsia pesca da OGNI riga 1X2
    aperta della finestra mobile 24h (qualsiasi status, qualsiasi quota > 1),
    deduplica per evento (miglior EV di oracolo) e ripete SOLO i gate che NON
    sono il filtro di prezzo bypassato:

    - esito canonico (`_canonical_esito`),
    - kickoff noto e in finestra (fail-closed come la corsia storica),
    - gate di lega (`value_filter.league_allowed`), con i nomi normalizzati
      (`canonical_league`) per non rifiutare una lega ammessa per come la
      scrive la fonte (lezione 24/09),
    - extra EV in probation (`TOP_DOWN_PROBATION_EXTRA`), in linea con la
      direttiva anti-spread del 21/09: il gate di prezzo e' bypassato, la
      prudenza sulle leghe non ancora validate no.

    NON tocca il filtro di prezzo: quota, prob. di mercato e tier restano nel
    pick come telemetria. La corsia SIM e la scrittura bottom-up del ledger
    sono INVARIATE: e' un cambio della corsia di selezione live, non del
    ledger (era del 22/09 intatta).

    Fail-safe: qualunque errore ritorna [] (come la corsia multi-mercato).
    Si spegne con TOP_DOWN_BYPASS=0 e si usa solo quando TOP_DOWN_EV e' ON.
    """
    if not (TOP_DOWN_EV and TOP_DOWN_BYPASS):
        return []
    try:
        from tracker import _get_conn
        from value_filter import league_allowed, canonical_league
        conn = _get_conn()
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        start = now_utc.isoformat().replace("+00:00", "Z")
        end = (now_utc + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        rows = c.execute('''SELECT m.id, m.home_team, m.away_team,
                                   m.commence_time, m.league, p.esito,
                                   p.quota, p.market_edge, p.market_prob,
                                   p.ev, p.status
                            FROM matches m JOIN predictions p ON m.id = p.match_id
                            WHERE m.commence_time >= ? AND m.commence_time < ?
                              AND p.mercato = '1X2'
                              AND p.esito_finale IS NULL
                              AND p.quota > 1.0
                            ORDER BY p.ev DESC''',
                         (start, end)).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("auto_bet: corsia top-down non disponibile (%s)", e)
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for (mid, home, away, commence, league, esito, quota,
         m_edge, m_prob, ev, status) in rows:
        if mid in seen:
            continue                      # un pick per evento
        canon = _canonical_esito(esito, home, away)
        if not canon:
            continue
        if not commence:
            logger.debug("auto_bet: top-down skip %s (kickoff assente)", mid)
            continue
        league_name = canonical_league((league or "").strip())
        if not league_name:
            logger.debug("auto_bet: top-down skip %s (lega assente: gate "
                         "fail-closed)", mid)
            continue
        if not league_allowed(league_name):
            logger.debug("auto_bet: top-down skip %s (lega '%s' fuori dai "
                         "campionati ammessi)", mid, league_name)
            continue
        seen.add(mid)
        out.append({"match_id": mid, "home": home, "away": away,
                    "commence": commence, "league": league or "",
                    "mercato": "1X2", "esito_raw": esito,
                    "quota": float(quota or 0),
                    "market_edge": float(m_edge) if m_edge is not None else None,
                    "market_prob": float(m_prob) if m_prob is not None else None,
                    "best_ev": float(ev) if ev is not None else 0.0,
                    "status": status or "rejected",
                    "top_down_lane": True,
                    **canon})
    if out:
        logger.info("auto_bet: corsia top-down: %d candidati 1X2 (bypass "
                    "fascia bottom-up, gate oracolo)", len(out))
    return out


def _too_close_to_start(start_time: str | None) -> bool:
    if not start_time:
        return False
    try:
        start = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except Exception:
        return False
    return start <= datetime.now(timezone.utc) + timedelta(minutes=MIN_MINUTES_TO_START)


def _kill_switch_override() -> str | None:
    """Override persistente scritto da /autobet (kill-switch Telegram).

    None se non impostato. Il file vive in data/execution/ (volume
    condiviso) per sopravvivere ai redeploy.
    """
    try:
        data = json.loads(KILL_SWITCH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    mode = str(data.get("mode", "")).strip().lower()
    return mode if mode in KILL_SWITCH_VALUES else None


def set_kill_switch(mode: str) -> dict:
    """Imposta l'override del kill-switch (comando Telegram /autobet).

    mode: "off" (stop totale), "sim" (pausa ordini reali),
    "live" (ripristina AUTO_BET_MODE env). Scrittura atomica sul volume.
    """
    mode = str(mode).strip().lower()
    if mode == "real":
        mode = "live"
    if mode not in KILL_SWITCH_VALUES:
        raise ValueError(
            f"modalita' non valida: {mode!r} (attese: off|sim|live)")
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"mode": mode,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    tmp = KILL_SWITCH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, KILL_SWITCH_FILE)
    return data


def clear_kill_switch() -> None:
    """Rimuove l'override: si torna ad AUTO_BET_MODE env."""
    try:
        KILL_SWITCH_FILE.unlink()
    except FileNotFoundError:
        pass


def kill_switch_status() -> dict:
    """Stato del kill-switch per /autobet (Telegram)."""
    override = _kill_switch_override()
    env_mode = os.getenv("AUTO_BET_MODE", "sim").strip().lower()
    requested = override or env_mode
    if override == "off":
        effective = "off"
    elif requested in REAL_MODE_VALUES and _provider_ready():
        effective = "live"
    else:
        effective = "sim"
    return {
        "override": override,
        "env_mode": env_mode,
        "requested": requested,
        "effective": effective,
        "provider_ready": _provider_ready(),
        "file": str(KILL_SWITCH_FILE),
    }


def _requested_mode() -> str:
    """Modalita' richiesta: override del kill-switch Telegram se presente,
    altrimenti env AUTO_BET_MODE (default 'sim')."""
    override = _kill_switch_override()
    if override:
        return override
    return os.getenv("AUTO_BET_MODE", "sim").strip().lower()


def _parse_iso_utc(s) -> "datetime | None":
    """ISO-8601 -> datetime UTC aware (None se non parsabile)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_daily_stop() -> dict:
    try:
        data = json.loads(DAILY_STOP_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_daily_stop(data: dict) -> None:
    DAILY_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_STOP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, DAILY_STOP_FILE)


# --- GATE DI MERCATO (15/09/2026) -----------------------------------------
# Il percorso che ORDINA non deve mai partire senza dati di mercato verificati:
# era l'anello mancante fra il contratto (`decision/market.py`) e la catena
# (`decision/feeds.py`, sorgente PRIMARIA SX Bet). Lo stato dell'ultimo verdetto
# resta in memoria di processo, cosi' il job del bot puo' notificarlo senza
# rileggere il feed (stesso processo: nessuna rete aggiuntiva).
_last_market_gate: dict = {"blocked": False, "reason": "", "detail": "",
                           "checked_at": None}


def _market_feed_gate(*, request_id: str = "auto_bet") -> tuple[bool, str, dict]:
    """Verdetto del feed di mercato per il percorso d'ordine (FAIL-CLOSED).

    Ritorna `(allowed, reason, identity)`. Qualunque problema — feed disattivato
    senza sorgenti, refresh fallito, quotatura vecchia, validazione incompleta,
    oppure un'eccezione imprevista — vale come BLOCCO: in assenza di certezza
    sul mercato non si punta. Si disattiva solo in modo esplicito con
    `DECISION_FEED_ENABLED=0` (scelta tracciata nei log, non silenziosa).
    """
    try:
        from decision.feeds import feed_enabled, feed_from_env
        if not feed_enabled():
            return True, "feed di mercato disattivato (DECISION_FEED_ENABLED=0)", {}
        feed = feed_from_env()
        if feed is None or not feed.has_sources:
            return False, "nessuna sorgente di mercato configurata", {}
        snapshot = feed.refresh(request_id=request_id)
        gate = feed.gate(snapshot)
        identity = dict(gate.identity or {})
        identity["validated"] = bool(snapshot.is_validated)
        identity["accepted"] = snapshot.accepted
        identity["age_s"] = round(snapshot.age_seconds(), 1)
        if not gate.allowed:
            return False, f"{gate.reason.value}: {gate.detail}", identity
        return True, gate.detail, identity
    except Exception as exc:                        # fail-closed anche qui
        return False, f"gate di mercato non valutabile ({type(exc).__name__}: {exc})", {}


def market_gate_status() -> dict:
    """Ultimo verdetto del gate di mercato (per /autobet e per le notifiche)."""
    return dict(_last_market_gate)


def clear_daily_stop() -> None:
    """Azzera lo stop-loss giornaliero (riattiva le puntate)."""
    try:
        DAILY_STOP_FILE.unlink()
    except FileNotFoundError:
        pass


def daily_stop_status() -> dict:
    """Stato dello stop-loss per /autobet: bloccato finche' `now < until`."""
    now = datetime.now(timezone.utc)
    data = _load_daily_stop()
    until = _parse_iso_utc(data.get("stopped_until"))
    return {
        "stopped": bool(until and now < until),
        "until": until.isoformat() if until else None,
        "day": data.get("day"),
        "start_bankroll": data.get("start_bankroll"),
        "basis_key": data.get("basis_key"),
        "stopped_at": data.get("stopped_at"),
        "reason": data.get("reason"),
        "loss_pct": DAILY_STOP_LOSS_PCT * 100,
        "hours": DAILY_STOP_HOURS,
        "file": str(DAILY_STOP_FILE),
    }


# Priorita' delle BASI di misura del bankroll (piu' alto = piu' autorevole).
# Serve a NON confrontare MAI grandezze diverse: il 21/09/2026 lo stop-loss
# aveva letto l'equity del wallet al mattino (33.5535) e la cassa simulata
# nel pomeriggio (20.00, dopo un errore transitorio di lettura del wallet),
# innescando un -40.4% INESISTENTE che ha bloccato le puntate per 24h col
# wallet INTATTO. Un errore di rete non e' una perdita.
BASIS_PRIORITY = {"live_equity": 2, "cassa": 1}


def _basis_priority(key: "str | None") -> int:
    """Priorita' della base di misura (`live_equity` > `cassa` > ignota)."""
    return BASIS_PRIORITY.get(key or "", 0)


def check_daily_stop(bankroll: float | None,
                     basis: str = "bankroll",
                     basis_key: "str | None" = None) -> dict:
    """Registra il bankroll di inizio giornata e blocca se perde >= pct.

    `bankroll` e' il valore di RISCHIO del giorno: in LIVE il chiamante passa
    l'EQUITY del wallet (disponibile + in gioco), mai il solo disponibile —
    vedi il commento del blocco DAILY_STOP. `basis` e' l'etichetta di quel
    valore nei log/messaggi ("equity wallet" oppure "cassa"); `basis_key` ne
    e' la chiave STABILE ("live_equity"/"cassa") registrata sul file.

    Il confronto avviene SOLO fra letture della STESSA base: se il wallet non
    e' leggibile il chiamante ripiega sulla cassa, e quella lettura NON viene
    confrontata col riferimento del giorno preso dall'equity. Una base PIU'
    autorevole RI-ARMA il riferimento (upgrade); una MENO autorevole viene
    ignorata e loggata (nessun trigger).

    Ritorna {stopped, start_bankroll, loss_pct, until, just_triggered}
    (+ `basis_mismatch`/`basis_changed` quando applicano).
    Fail-open: un errore di lettura/scrittura NON blocca le puntate (meglio
    puntare che fermarsi per un file corrotto), ma il blocco attivo resta
    rispettato.
    """
    now = datetime.now(timezone.utc)
    try:
        data = _load_daily_stop()
        until = _parse_iso_utc(data.get("stopped_until"))
        if until is not None and now < until:
            return {"stopped": True, "until": until.isoformat(),
                    "start_bankroll": data.get("start_bankroll"),
                    "loss_pct": None, "just_triggered": False,
                    "basis_key": data.get("basis_key")}
        if DAILY_STOP_LOSS_PCT <= 0 or not bankroll or bankroll <= 0:
            return {"stopped": False, "loss_pct": None,
                    "just_triggered": False}
        today = now.date().isoformat()
        if data.get("day") != today or not data.get("start_bankroll"):
            _save_daily_stop({"day": today, "start_bankroll": float(bankroll),
                              "stopped_until": None,
                              "basis_key": basis_key or basis})
            return {"stopped": False, "start_bankroll": float(bankroll),
                    "loss_pct": 0.0, "just_triggered": False,
                    "basis_key": basis_key or basis}
        stored_key = data.get("basis_key") or basis
        if basis_key and stored_key != basis_key:
            if _basis_priority(basis_key) > _basis_priority(stored_key):
                # Lettura da una base PIU' autorevole (es. la cassa del primo
                # giro e poi il wallet reale): il riferimento del giorno si
                # ri-arma sul valore buono, senza confrontare le due basi.
                data.update({"start_bankroll": float(bankroll),
                             "basis_key": basis_key,
                             "stopped_until": None})
                _save_daily_stop(data)
                logger.warning(
                    "auto_bet: stop-loss giorno ri-armato su base piu' "
                    "autorevole (%s -> %s, valore %.2f)",
                    stored_key, basis_key, bankroll)
                return {"stopped": False, "start_bankroll": float(bankroll),
                        "loss_pct": 0.0, "just_triggered": False,
                        "basis_changed": True, "basis_key": basis_key}
            # Base MENO autorevole (wallet illeggibile -> cassa): nessun
            # confronto e nessun trigger. Il riferimento del giorno resta
            # quello della base autorevole.
            logger.warning(
                "auto_bet: stop-loss NON valutato — lettura sulla base '%s' "
                "(valore %.2f) mentre il riferimento del giorno e' '%s'",
                basis_key, bankroll, stored_key)
            return {"stopped": False, "loss_pct": None,
                    "just_triggered": False, "basis_mismatch": True,
                    "basis_key": stored_key}
        start = float(data["start_bankroll"])
        loss = (start - float(bankroll)) / start if start > 0 else 0.0
        if loss >= DAILY_STOP_LOSS_PCT:
            until = now + timedelta(hours=DAILY_STOP_HOURS)
            data.update({"stopped_until": until.isoformat(),
                         "stopped_at": now.isoformat(),
                         "start_bankroll": start,
                         "reason": f"{basis} -{loss * 100:.1f}% dall'inizio "
                                   f"giornata (valore {bankroll:.2f})"})
            _save_daily_stop(data)
            logger.error("auto_bet: STOP-LOSS GIORNALIERO — perdita %.1f%% "
                         "(>= %.0f%%): puntate bloccate fino a %s",
                         loss * 100, DAILY_STOP_LOSS_PCT * 100,
                         until.isoformat())
            return {"stopped": True, "until": until.isoformat(),
                    "start_bankroll": start, "loss_pct": loss * 100,
                    "just_triggered": True}
        return {"stopped": False, "start_bankroll": start,
                "loss_pct": loss * 100, "just_triggered": False}
    except Exception as e:
        logger.warning("auto_bet: check_daily_stop fallito (%s), fail-open", e)
        return {"stopped": False, "loss_pct": None, "just_triggered": False}


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER SETTIMANALE (26/09/2026) — drawdown ROLLING 7 giorni
# ---------------------------------------------------------------------------

def _load_json_dict(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json_dict(path, data: dict) -> None:
    """Scrittura ATOMICA (tmp + os.replace) di un file JSON sul volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_history() -> dict:
    return _load_json_dict(BANKROLL_HISTORY_FILE)


def _prune_history(samples, now, window_h) -> list:
    """Tiene solo i campioni dentro la finestra (ts ISO -> datetime UTC)."""
    cutoff = now - timedelta(hours=float(window_h))
    out: list = []
    for item in samples or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts = _parse_iso_utc(item[0])
        try:
            val = float(item[1])
        except (TypeError, ValueError):
            continue
        if ts is None or ts < cutoff or val <= 0:
            continue
        out.append([ts.isoformat(), val])
    return out


def record_bankroll_sample(bankroll, basis_key=None, now=None) -> dict:
    """Aggiunge un campione di equity allo storico rolling (max 1/ora).

    La scrittura e' FAIL-SAFE: un errore non ferma il giro (lo storico e' una
    telemetria di sicurezza, non deve bloccare le puntate). Una base MENO
    autorevole (cassa dopo un errore di lettura del wallet) NON viene
    registrata: mischiare equity e cassa avrebbe prodotto un falso drawdown
    (stessa classe di bug del 21/09/2026 sullo stop giornaliero).
    """
    out = {"recorded": False, "samples": 0}
    try:
        now = now or datetime.now(timezone.utc)
        if not bankroll or float(bankroll) <= 0 or WEEKLY_STOP_WINDOW_H <= 0:
            return out
        data = _load_history()
        key = data.get("basis_key")
        if basis_key and key and key != basis_key:
            if _basis_priority(basis_key) < _basis_priority(key):
                return {"recorded": False, "samples": 0,
                        "basis_mismatch": True}
            data = {"basis_key": basis_key, "samples": []}
        elif basis_key and not key:
            data["basis_key"] = basis_key
        samples = _prune_history(data.get("samples"), now, WEEKLY_STOP_WINDOW_H)
        last_ts = _parse_iso_utc(samples[-1][0]) if samples else None
        if last_ts is None or \
                (now - last_ts).total_seconds() >= WEEKLY_SAMPLE_MIN_SECONDS:
            samples.append([now.isoformat(), float(bankroll)])
        data["samples"] = samples
        _save_json_dict(BANKROLL_HISTORY_FILE, data)
        out = {"recorded": True, "samples": len(samples)}
    except Exception as e:
        logger.debug("auto_bet: record_bankroll_sample fallito (%s)", e)
    return out


def weekly_drawdown(bankroll=None, now=None) -> dict:
    """Drawdown ROLLING dal picco delle ultime WEEKLY_STOP_WINDOW_H ore.

    `drawdown_pct` e' una percentuale (0-100). Il valore corrente partecipa al
    picco, cosi' non e' mai inferiore all'ultima lettura.
    """
    now = now or datetime.now(timezone.utc)
    data = _load_history()
    samples = _prune_history(data.get("samples"), now, WEEKLY_STOP_WINDOW_H)
    values = [v for _, v in samples]
    if bankroll and float(bankroll) > 0:
        values.append(float(bankroll))
    peak = max(values) if values else None
    current = float(bankroll) if bankroll else (values[-1] if values else None)
    dd = 0.0
    if peak and peak > 0 and current is not None:
        dd = max(0.0, (peak - current) / peak)
    return {"drawdown_pct": round(dd * 100.0, 2), "peak": peak,
            "current": current, "n_samples": len(values),
            "window_h": WEEKLY_STOP_WINDOW_H,
            "basis_key": data.get("basis_key")}


def weekly_stop_status() -> dict:
    """Stato del circuit breaker settimanale (per /autobet e i report)."""
    now = datetime.now(timezone.utc)
    data = _load_json_dict(WEEKLY_STOP_FILE)
    until = _parse_iso_utc(data.get("stopped_until"))
    stopped = bool(until and now < until)
    return {
        "stopped": stopped,
        "until": until.isoformat() if until else None,
        "stopped_at": data.get("stopped_at"),
        "reason": data.get("reason"),
        "peak": data.get("peak"),
        "drawdown_pct": data.get("drawdown_pct"),
        "basis_key": data.get("basis_key"),
        "loss_pct": WEEKLY_STOP_LOSS_PCT * 100,
        "hours": WEEKLY_STOP_HOURS,
        "window_h": WEEKLY_STOP_WINDOW_H,
        "file": str(WEEKLY_STOP_FILE),
    }


def clear_weekly_stop() -> None:
    """Azzera il blocco settimanale (riattiva le puntate)."""
    try:
        WEEKLY_STOP_FILE.unlink()
    except FileNotFoundError:
        pass


def check_weekly_stop(bankroll, basis: str = "bankroll",
                      basis_key: "str | None" = None,
                      now=None) -> dict:
    """Registra il campione e blocca se il drawdown rolling >= soglia.

    Ritorna {stopped, drawdown_pct, peak, just_triggered, until}
    (+ `basis_mismatch` quando applica). Fail-open su errore (come il daily):
    un file corrotto non deve fermare il portafoglio, ma un blocco attivo
    resta rispettato.
    """
    now = now or datetime.now(timezone.utc)
    try:
        data = _load_json_dict(WEEKLY_STOP_FILE)
        until = _parse_iso_utc(data.get("stopped_until"))
        if until is not None and now < until:
            return {"stopped": True, "until": until.isoformat(),
                    "drawdown_pct": data.get("drawdown_pct"),
                    "peak": data.get("peak"), "just_triggered": False,
                    "basis_key": data.get("basis_key")}
        if WEEKLY_STOP_LOSS_PCT <= 0 or not bankroll or float(bankroll) <= 0:
            return {"stopped": False, "just_triggered": False,
                    "drawdown_pct": None}
        rec = record_bankroll_sample(bankroll, basis_key=basis_key, now=now)
        if rec.get("basis_mismatch"):
            logger.warning("auto_bet: stop settimanale NON valutato — lettura "
                           "su base '%s' meno autorevole dello storico", basis_key)
            return {"stopped": False, "just_triggered": False,
                    "drawdown_pct": None, "basis_mismatch": True}
        dd = weekly_drawdown(bankroll, now=now)
        if dd["drawdown_pct"] / 100.0 >= WEEKLY_STOP_LOSS_PCT:
            until = now + timedelta(hours=WEEKLY_STOP_HOURS)
            _save_json_dict(WEEKLY_STOP_FILE, {
                "stopped_until": until.isoformat(),
                "stopped_at": now.isoformat(),
                "peak": dd["peak"], "drawdown_pct": dd["drawdown_pct"],
                "basis_key": basis_key or dd.get("basis_key"),
                "reason": f"{basis} -{dd['drawdown_pct']:.1f}% dal picco "
                          f"rolling {WEEKLY_STOP_WINDOW_H:.0f}h "
                          f"(picco {dd['peak']:.2f}, ora {dd['current']:.2f})",
            })
            logger.error("auto_bet: STOP-LOSS SETTIMANALE — drawdown %.1f%% "
                         "(>= %.0f%%): puntate bloccate fino a %s",
                         dd["drawdown_pct"], WEEKLY_STOP_LOSS_PCT * 100,
                         until.isoformat())
            return {"stopped": True, "until": until.isoformat(),
                    "drawdown_pct": dd["drawdown_pct"], "peak": dd["peak"],
                    "just_triggered": True,
                    "basis_key": basis_key or dd.get("basis_key")}
        return {"stopped": False, "drawdown_pct": dd["drawdown_pct"],
                "peak": dd["peak"], "just_triggered": False,
                "basis_key": basis_key or dd.get("basis_key")}
    except Exception as e:
        logger.warning("auto_bet: check_weekly_stop fallito (%s), fail-open", e)
        return {"stopped": False, "just_triggered": False, "drawdown_pct": None}


def _provider_ready() -> bool:
    """True se execution_engine puo' piazzare ordini REALI (provider
    selezionato da EXECUTION_PROVIDER + credenziali, nessun DryRun)."""
    try:
        import execution_engine as ee
    except Exception:
        return False
    try:
        if ee.EXECUTION_DRY_RUN:
            return False
        if ee.EXECUTION_PROVIDER not in ("sxbet", "smarkets",
                                         "betinasia", "mollybet"):
            return False
        return bool(ee._creds_configured())
    except Exception:
        return False


def _live_wallet_snapshot() -> "dict | None":
    """Istantanea del wallet reale: disponibile, in gioco ed EQUITY (USDC).

    Ritorna `{"available", "exposure", "equity"}` oppure None se il wallet
    non e' leggibile (dry-run, errore di rete, credenziali assenti): in quel
    caso il chiamante ripiega sul bankroll cassa.

    - `available` = saldo libero, spendibile per un NUOVO ordine (vincolo di
      cassa);
    - `exposure` = fondi in escrow (bet aperte) + prenotati;
    - `equity` = available + exposure: il PATRIMONIO del wallet. E' l'unico
      valore che non si muove quando una bet passa da libera a "in gioco",
      quindi e' il riferimento per Kelly, drawdown e stop-loss.

    Se il provider non espone `exposure` la stima e' PRUDENTE (equity = solo
    disponibile): lo stop-loss puo' scattare un po' prima, mai dopo — resta
    la direzione fail-closed.
    """
    try:
        import execution_engine as ee
        engine = ee.ExecutionEngine()
        if isinstance(engine.provider, ee.DryRunProvider):
            return None
        bal = engine.provider.get_balance()
        available = float(bal.get("availableBalance") or 0.0)
        raw_exposure = bal.get("exposure")
        if raw_exposure is None:
            logger.warning("auto_bet: wallet senza campo 'exposure' — equity "
                           "= solo disponibile (stima PRUDENTE: lo stop-loss "
                           "puo' scattare prima)")
            exposure = 0.0
        else:
            exposure = float(raw_exposure or 0.0)
        return {"available": available, "exposure": exposure,
                "equity": available + exposure}
    except Exception as e:
        logger.warning("auto_bet: lettura saldo wallet fallita: %s", e)
        return None


def _execution_mode(allow_sim: bool = True) -> str:
    """Modalita' effettiva del giro di puntate.

    'live' solo se AUTO_BET_MODE richiede l'esecuzione reale E il provider
    e' configurato (mai ordini reali senza configurazione esplicita).
    Altrimenti 'sim' (fallback di default) oppure 'off' (fail-closed
    richiesto dal chiamante).
    """
    mode = _requested_mode()
    if mode == "off":
        # Kill-switch /autobet off: STOP TOTALE, mai puntate (ne' reali ne'
        # simulate) finche' l'admin non riattiva.
        logger.warning("auto_bet: kill-switch OFF attivo — nessuna puntata")
        return "off"
    if mode in REAL_MODE_VALUES:
        if _provider_ready():
            return "live"
        logger.warning(
            "auto_bet: AUTO_BET_MODE=%s ma nessun provider reale configurato "
            "(EXECUTION_PROVIDER + credenziali) -> %s",
            mode, "SIM (fallback)" if allow_sim else "nessuna puntata")
        return "sim" if allow_sim else "off"
    return "sim"


def _live_available_size(prov, market_id: str, selection_id: int,
                         min_price: float) -> float | None:
    """Profondita' BACK disponibile a prezzo >= `min_price` (USDC/stake).

    None se il book non e' leggibile o in un formato non noto (il chiamante
    NON blocca in quel caso: la guardia liquidita' scatta solo con un dato
    reale). Su un exchange un mercato sottile produce slippage o riempimenti
    parziali: se la size al floor e' inferiore allo stake, si salta.
    """
    get_book = getattr(prov, "get_market_book", None)
    if get_book is None:
        return None
    try:
        book = get_book(market_id)
    except Exception:
        return None
    for r in (book or {}).get("runners", []) or []:
        sid = r.get("selectionId", r.get("selection_id"))
        try:
            if int(sid) != int(selection_id):
                continue
        except (TypeError, ValueError):
            continue
        levels = r.get("availableToBack") or r.get("available_to_back")
        if not isinstance(levels, list):
            return None  # book in formato non noto: non bloccare
        total = 0.0
        for lv in levels:
            try:
                p = float(lv.get("price") or 0)
                size = float(lv.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if p + 1e-9 >= min_price:
                total += size
        return total
    return None


def _live_fill(pick: dict, stake: float, floor: float) -> dict | None:
    """Piazza un ordine REALE per il segnale e ritorna l'esito.

    Flusso (fail-closed, nessuna eccezione verso il chiamante):
    1. engine reale (provider da env, mai DryRun);
    2. risoluzione del mercato exchange per la STESSA partita ed esito
       (execution_engine.resolve_match_market: nomi squadre + kickoff,
       univocita' — niente ordini su eventi ambigui);
    3. floor EV: se il miglior prezzo disponibile e' sotto la quota del
       segnale la puntata non e' piu' +EV -> salto;
    4. ordine IOC con bound = quota segnale (riempimento a quella quota o
       meglio: l'edge e' stato calcolato su quella quota).

    Ritorna None per i salti (mercato non trovato/ambiguo, prezzo sotto il
    floor, errore di rete) e per gli ordini riusciti un dict con
    {ok, market_id, selection_id, bet_id, status, price, stake}.
    """
    if DRY_RUN:
        logger.warning("auto_bet: DRY-RUN — _live_fill INTERCETTATO per %s "
                       "(%s): nessun POST a SX Bet",
                       pick.get("match_id"), pick.get("esito_key"))
        return None
    try:
        import execution_engine as ee
    except Exception as e:
        logger.warning("auto_bet: execution_engine non disponibile (%s), "
                       "salto %s", e, pick.get("match_id"))
        return None
    try:
        engine = ee.ExecutionEngine()
    except Exception as e:
        logger.warning("auto_bet: ExecutionEngine non avviato (%s), salto %s",
                       e, pick.get("match_id"))
        return None
    if isinstance(engine.provider, ee.DryRunProvider):
        logger.warning("auto_bet: provider in DryRun, nessun ordine reale "
                       "per %s (%s)", pick.get("match_id"),
                       pick.get("esito_key"))
        return None
    prov = engine.provider

    # Multi-mercato (19/09/2026): OU e AH non vivono nel catalogo 1X2 (3
    # mercati binari "X vs Not X"): servono TYPE ID e LINEA, quindi un
    # resolver dedicato. Fail-closed: se il pick non e' riconducibile a un
    # mercato a linea NON si indovina — nessun ordine.
    # Corsia eSports (30/09/2026): mercato binario 2 VIE (type 52) sullo sport
    # 9. Si compra il lato `team`, risolto per NOME e non per "1"/"2": su SX
    # l'esito 1|2 e' una convenzione del 1X2 e non esiste per un moneyline.
    is_ml = str(pick.get("mercato") or "").upper() == "ML"
    if is_ml and not str(pick.get("team") or "").strip():
        logger.warning("auto_bet: pick eSports %s senza il lato da comprare, "
                       "salto (fail-closed)", pick.get("match_id"))
        return None
    # Corsia TENNIS (30/09/2026): il pick porta GIA' market_id/selection_id di
    # SX. La struttura del tennis e' UN mercato per match coi lati sulle chiavi
    # 1/2: non si puo' ri-risolvere con `resolve_moneyline_market` (che cerca
    # un mercato per SQUADRA, come gli eSports) ne' col resolver 1X2. Fail-closed
    # se l'identita' del mercato non c'e': meglio nessun ordine che uno sul
    # lato sbagliato.
    is_tennis = str(pick.get("mercato") or "").upper() == "TENNIS"
    mkt = None
    if is_tennis:
        mid_t = str(pick.get("market_id") or "").strip()
        sel_t = pick.get("selection_id")
        if not mid_t or sel_t not in (1, 2, "1", "2"):
            logger.warning("auto_bet: pick tennis %s senza market_id/selection "
                           "validi, salto (fail-closed)", pick.get("match_id"))
            return None
        mkt = {"market_id": mid_t, "selection_id": int(sel_t),
               "event_name": f"{pick.get('home')} vs {pick.get('away')}",
               "label": str(pick.get("team") or ""),
               "market_type": "ML", "line": None, "provider": "sxbet"}
    target = None
    if not is_tennis and str(pick.get("mercato") or "").upper() in ("OU", "AH"):
        try:
            from multi_market import order_target
            target = order_target(pick)
        except Exception:
            target = None
        if target is None or target.get("line") is None:
            logger.info("auto_bet: %s (%s) mercato a linea non "
                        "riconoscibile, salto", pick.get("match_id"),
                        pick.get("esito_key"))
            return None
    try:
        if mkt is not None:
            pass                      # tennis: identita' gia' nel pick
        elif is_ml:
            mkt = ee.resolve_moneyline_market(
                prov, pick["home"], pick["away"], pick["team"],
                pick.get("commence"))
        elif target is not None:
            mkt = ee.resolve_market_for(
                prov, pick["home"], pick["away"], target["market_type"],
                target["line"], target["side"], pick.get("commence"))
        else:
            mkt = ee.resolve_match_market(
                prov, pick["home"], pick["away"], pick["esito_key"],
                pick.get("commence"))
    except Exception as e:
        logger.warning("auto_bet: risoluzione mercato %s fallita: %s",
                       pick.get("match_id"), e)
        return None
    if not mkt:
        logger.info("auto_bet: mercato non trovato/ambiguo su %s per "
                    "%s vs %s (%s), salto",
                    getattr(prov, "name", "?"), pick["home"],
                    pick["away"], pick["esito_key"])
        return None
    market_id, sel = mkt["market_id"], mkt["selection_id"]

    best = None
    try:
        best = prov.best_back_price(market_id, sel)
    except Exception:
        best = None
    if best is not None and best < float(floor) - 1e-9:
        logger.info("auto_bet: %s vs %s (%s): best %s %.2f < floor segnale "
                    "%.2f, salto (EV perso)", pick["home"], pick["away"],
                    pick["esito_key"], getattr(prov, "name", "?"),
                    best, float(floor))
        return None

    # Guardia liquidita' (tarata 11/09): il prezzo migliore puo' avere size
    # inferiore allo stake -> slippage o riempimento parziale. Si pretende la
    # copertura dello stake CON MARGINE (stake x SX_DEPTH_MULTIPLIER) e una
    # profondita' assoluta minima (SX_MIN_EXEC_DEPTH_USDC). Si salta solo se
    # il book E' leggibile: se il formato e' ignoto NON si blocca (fail-open
    # sulla lettura, fail-closed sulla size reale effettivamente misurata).
    need = required_depth(float(stake))
    depth = _live_available_size(prov, market_id, sel, float(floor))
    if depth is not None and depth + 1e-9 < need:
        logger.info("auto_bet: %s vs %s (%s): liquidita' %.2f < richiesta "
                    "%.2f (stake %.2f x %.2f, minimo %.2f) al floor %.2f, "
                    "salto (rischio slippage)",
                    pick["home"], pick["away"], pick["esito_key"],
                    depth, need, float(stake), SX_DEPTH_MULTIPLIER,
                    MIN_EXEC_DEPTH_USDC, float(floor))
        # Monitor scarti (11/09): l'ordine e' saltato per book sottile.
        # Traccia anche l'edge NON realizzato (EV x stake target): e' la
        # metrica che quantifica il costo dello slippage evitato.
        try:
            from liquidity_monitor import record_skip
            record_skip("order", "depth_vs_stake",
                        match_id=pick.get("match_id"),
                        home=pick.get("home"), away=pick.get("away"),
                        esito=pick.get("esito_key"), quota=float(floor),
                        depth=depth, threshold=need,
                        stake=float(stake), ev=pick.get("best_ev"),
                        extra={"market_id": market_id,
                               "selection_id": sel,
                               "provider": getattr(prov, "name", "?"),
                               "richiesto": round(need, 2),
                               "multiplier": SX_DEPTH_MULTIPLIER,
                               "min_exec_depth": MIN_EXEC_DEPTH_USDC})
        except Exception:
            pass
        return None

    try:
        order = prov.place_limit_order(market_id, sel, "BACK",
                                       float(floor), float(stake))
    except Exception as e:
        logger.warning("auto_bet: ordine reale %s fallito (%s vs %s): %s",
                       market_id, pick["home"], pick["away"], e)
        return None

    matched_price = (float(order.price_matched)
                     if order.price_matched else float(floor))
    matched_stake = float(order.size_matched or 0.0)
    if not order.ok or matched_stake <= 0:
        logger.warning("auto_bet: ordine reale %s non riempito (%s vs %s, "
                       "%s): %s", market_id, pick["home"], pick["away"],
                       order.status, order.error or "nessun match")
        return {"ok": False, "market_id": market_id,
                "selection_id": sel, "status": order.status or "FAILURE",
                "error": order.error}

    # CONFERMA OBBLIGATORIA DEL BET ID (difesa in profondita'): lo stato
    # "riempito" senza un id emesso dall'exchange NON e' verificabile
    # sull'interfaccia reale. Un ordine non verificabile non deve diventare
    # una riga "piazzata" sul ledger (che verrebbe poi saldata come denaro
    # vero): fail-closed, nessun salvataggio a valle.
    if not order.bet_id:
        logger.error("auto_bet: ordine %s (%s vs %s, %s) riempito (%s, "
                     "%.2f USDC) ma SENZA bet_id: NON registrato come "
                     "piazzato (fail-closed)", market_id, pick["home"],
                     pick["away"], pick.get("esito_key"), order.status,
                     matched_stake)
        return {"ok": False, "market_id": market_id, "selection_id": sel,
                "bet_id": order.bet_id,
                "status": order.status or "NO_BET_ID",
                "price": matched_price, "stake": matched_stake,
                "error": "ordine senza bet_id: non confermabile "
                         "sull'exchange"}
    logger.info("auto_bet: ORDINE REALE riempito %s (%s vs %s, %s) @ %.2f "
                "per €%.2f [%s]", market_id, pick["home"], pick["away"],
                pick["esito_key"], matched_price, matched_stake, order.status)
    # Monitor scarti (11/09): riempimento PARZIALE = parte dello stake non
    # eseguita (liquido insufficiente oltre il primo livello).
    if matched_stake + 1e-9 < float(stake):
        try:
            from liquidity_monitor import record_skip
            record_skip("partial", "riempimento_parziale",
                        match_id=pick.get("match_id"),
                        home=pick.get("home"), away=pick.get("away"),
                        esito=pick.get("esito_key"), quota=matched_price,
                        stake=round(float(stake) - matched_stake, 4),
                        ev=pick.get("best_ev"),
                        extra={"stake_richiesto": float(stake),
                               "stake_riempito": matched_stake,
                               "market_id": market_id})
        except Exception:
            pass
    # NB: niente fallback a "SUCCESS". Uno status inventato qui renderebbe
    # indistinguibile un riempimento confermato dall'exchange da una
    # risposta incompleta (il ledger e' un registro di denaro, non un log).
    return {"ok": True, "market_id": market_id, "selection_id": sel,
            "bet_id": order.bet_id,
            "status": order.status or "FILLED_UNCONFIRMED",
            "price": matched_price, "stake": matched_stake}


def _league_multiplier(league: str | None) -> float:
    """Moltiplicatore di Kelly per la lega (ponderazione CLV, default OFF).

    Delega a `adaptive_weighting.league_multiplier` (import pigro): l'env
    `ADAPTIVE_WEIGHTING` decide se ha effetto, e il modulo ritorna 1.0 quando
    e' spenta o quando il campione della lega e' insufficiente. **Va solo
    verso il basso**: non esiste un percorso che alzi lo stake. Fail-open:
    qualunque errore vale 1.0 — una telemetria rotta non deve cambiare lo
    stake (e un drawdown non deve mai essere inventato).
    """
    try:
        import adaptive_weighting
        return float(adaptive_weighting.league_multiplier(league))
    except Exception as e:
        logger.debug("auto_bet: ponderazione per lega non disponibile (%s)", e)
        return 1.0


def run_today_bets(stake_eur: float | None = None,
                   allow_sim: bool = True) -> list[dict]:
    """Piazza le puntate del giorno (SIM di default, LIVE con
    AUTO_BET_MODE=live + provider reale). Ritorna il riepilogo.

    In SIM usa la quota del segnale (paper trading) e registra in `bets`
    con mode='sim'. In LIVE risolve il mercato exchange e piazza un ordine
    reale (mode='live' con market_id/selection_id/bet_id); gli ordini non
    riempiti o i salti (mercato assente, prezzo sotto il floor EV) non
    lasciano righe sul ledger. Il saldo a fine partita e' sempre il solito
    (settle_bets via the-odds-api).

    Non lancia mai eccezioni verso il chiamante: ogni passo fallito viene
    loggato e saltato (fail-closed).

    Args:
        stake_eur: stake fisso di fallback (se adaptive_staking non
            disponibile). Default BET_STAKE_EUR env o 5.00.
        allow_sim: True -> in assenza di provider reale si ripiega sulla
            simulazione; False -> fail-closed (nessuna puntata).
    """
    # Default stake fisso (fallback se adaptive staking non disponibile)
    stake_eur_default = stake_eur if stake_eur is not None else float(
        os.getenv("BET_STAKE_EUR", str(BET_STAKE_DEFAULT_EUR)))

    # --- Modalita' effettiva del giro (prima dei candidati: decide il
    # --- bankroll del Kelly).
    mode = _execution_mode(allow_sim)
    if mode == "off":
        # Kill-switch /autobet off oppure fail-closed senza provider reale.
        logger.error("auto_bet: modalita' 'off' (kill-switch o fail-closed): "
                     "nessuna puntata")
        return []

    # Carica adaptive staking (lazy)
    try:
        from adaptive_staking import adaptive_stake, bankroll_stats
        _adaptive = True
        _bankroll_stats = bankroll_stats()
        # Cassa vuota (current=0) -> default €100, coerente con la dashboard
        # e con il comportamento pre-adaptive: mai puntare con bankroll 0.
        _bankroll = _bankroll_stats.get("current") or 100.0
        _peak = _bankroll_stats.get("peak") or _bankroll
    except ImportError:
        _adaptive = False
        _bankroll = 100.0
        _peak = 100.0
        logger.info("auto_bet: adaptive_staking non disponibile, uso stake fisso")

    # LIVE: il bankroll e' l'EQUITY del wallet REALE (disponibile + in gioco),
    # non la cassa simulata: il rischio si misura sul patrimonio, non sulla
    # cassa libera del momento. Il DISPONIBILE resta il vincolo di cassa del
    # singolo ordine (i fondi in escrow non sono spendibili). Se il wallet non
    # copre nemmeno il minimo ordine non si piazza nulla (fail-closed).
    _wallet_balance: float | None = None
    _wallet_exposure: float | None = None
    _wallet_equity: float | None = None   # riferimento del CB2 (kill switch)
    _spendable = _bankroll          # limite di cassa per singolo ordine
    if mode == "live":
        snapshot = _live_wallet_snapshot()
        if snapshot is None:
            logger.warning("auto_bet: saldo wallet non disponibile, uso "
                           "bankroll cassa €%.2f", _bankroll)
        elif snapshot["available"] < MIN_STAKE_EUR:
            logger.error("auto_bet: wallet sotto il minimo ordine "
                         "(%.2f USDC liberi < %.2f): nessuna puntata",
                         snapshot["available"], MIN_STAKE_EUR)
            return []
        else:
            _wallet_balance = snapshot["available"]
            _wallet_exposure = snapshot["exposure"]
            _wallet_equity = snapshot["equity"]
            _spendable = snapshot["available"]
            # Kelly, drawdown protection e stop-loss misurano l'EQUITY: cosi'
            # una bet piazzata (liberi -> escrow) non e' una perdita.
            _bankroll = snapshot["equity"]
            _peak = snapshot["equity"]
            logger.info("auto_bet: bankroll LIVE = equity %.2f USDC "
                        "(disponibile %.2f + in gioco %.2f)",
                        _bankroll, _wallet_balance, _wallet_exposure)

    # --- STOP-LOSS GIORNALIERO (11/09): nessuna puntata dopo un -5% dal
    # valore di inizio giornata, per DAILY_STOP_HOURS (default 24h). In LIVE
    # il riferimento e' l'EQUITY (disponibile + in gioco), non la cassa
    # libera: vedi il commento del blocco DAILY_STOP.
    # La BASE e' dichiarata con una chiave stabile: e' "live_equity" SOLO se
    # il wallet e' stato letto davvero. Quando la lettura fallisce il giro
    # ripiega sulla cassa, e quella lettura NON va mai confrontata col
    # riferimento preso dall'equity (21/09/2026: -40.4% inesistente).
    _stop_basis, _stop_basis_key = "cassa", "cassa"
    if mode == "live" and _wallet_equity is not None:
        _stop_basis, _stop_basis_key = "equity wallet", "live_equity"
    stop = check_daily_stop(_bankroll, basis=_stop_basis,
                            basis_key=_stop_basis_key)
    if stop.get("stopped"):
        logger.error("auto_bet: STOP-LOSS GIORNALIERO attivo fino a %s "
                     "(%s) — nessuna puntata", stop.get("until"),
                     stop.get("reason") or "perdita giornaliera")
        return []

    # --- CIRCUIT BREAKER SETTIMANALE (26/09): drawdown ROLLING 7g >= 12%.
    # Stessa base del daily (EQUITY in LIVE): un blocco qui ferma il giro.
    weekly = check_weekly_stop(_bankroll, basis=_stop_basis,
                               basis_key=_stop_basis_key)
    if weekly.get("stopped"):
        logger.error("auto_bet: STOP-LOSS SETTIMANALE attivo fino a %s "
                     "(drawdown %.1f%% dal picco %s) — nessuna puntata",
                     weekly.get("until"), weekly.get("drawdown_pct") or 0.0,
                     weekly.get("peak"))
        return []

    # --- CB2: KILL SWITCH PATRIMONIALE T-60 (17/09) — autorita' SUPERIORE a
    # qualunque altra considerazione. Se il flag e' armato (persistente, sul
    # volume) il sistema e' ARRESTATO: nessuna puntata in ALCUNA modalita'
    # finche' un admin non lo disinnesca (/t60reset). In LIVE il wallet viene
    # ricontrollato QUI: equity <= 30 USDC (o non leggibile) -> arresto
    # immediato + alert di emergenza Telegram (solo al primo innescio;
    # il job t60 del bot gestisce il promemoria persistente 1/giorno).
    if t60_kill_switch_status().get("triggered"):
        logger.error("auto_bet: T60 KILL SWITCH attivo (%s) — processo "
                     "arrestato: nessuna puntata",
                     t60_kill_switch_status().get("reason") or "soglia wallet")
        return []
    if mode == "live":
        if t60_check_wallet_kill(_wallet_equity):
            _t60_emergency_alert(
                t60_kill_switch_status().get("reason") or "soglia wallet")
            return []

    # --- RECINTO DI ESPOSIZIONE APERTA (27/09/2026): tetto sul CAPITALE
    # immobilizzato nelle puntate reali ancora aperte. Complementare (non
    # sostitutivo) a TOTAL_EXPOSURE_CAP_PCT, che misura i FLUSSI del giorno:
    # qui conta quanto e' davvero in gioco. Raggiunta la soglia il giro
    # DEGRADA a shadow: nessun ordine reale, ma classificazione, valutazione
    # e telemetria continuano a girare (e' cio' che deve accadere finche' le
    # partite in corso non sono saldate). Solo in LIVE: la cassa simulata non
    # immobilizza capitale reale.
    exposure_state = open_exposure_status(_bankroll)
    _exposure_blocked = bool(mode == "live" and exposure_state.get("blocked"))
    if _exposure_blocked:
        logger.error("auto_bet: RECINTO ESPOSIZIONE APERTA — %s",
                     exposure_state.get("reason"))
    elif mode == "live":
        logger.info("auto_bet: esposizione aperta %.2f/%.2f USDC su %s ordini "
                    "(cap %.0f%%, stake fisso %.2f USDC)",
                    exposure_state.get("open_stake") or 0.0,
                    exposure_state.get("cap") or 0.0,
                    exposure_state.get("count"),
                    OPEN_EXPOSURE_CAP_PCT * 100, fixed_order_stake())

    # Carica CLV storico per la confidenza
    try:
        from tracker import _get_conn as _gc
        _conn = _gc()
        _clv_row = _conn.execute(
            "SELECT AVG(CASE WHEN closing_quota > 0 "
            "THEN signal_quota / closing_quota - 1.0 ELSE 0 END) "
            "FROM clv_history").fetchone()
        _conn.close()
        _avg_clv = float(_clv_row[0]) if _clv_row and _clv_row[0] else 0.0
    except Exception:
        _avg_clv = 0.0

    from tracker import bet_exists_open
    from value_filter import (get_optimal_timing, calculate_exposure,
                                MOVEMENT_THRESHOLD, MOVEMENT_BONUS_MULTIPLIER,
                                EV_MIN, ODDS_MIN, ODDS_MAX, MARKET_EDGE_MIN)

    # --- Soglie lette dalle costanti di value_filter: il log non puo'
    # divergere dalla strategia reale (favoriti netti 1.30-1.80; edge minimo
    # +2pp sul mercato dal 21/09, era +3pp). ---
    # A DEBUG di proposito (21/09/2026): sono CONFIGURAZIONE, identiche a ogni
    # giro. Ripetute a INFO ogni 60s affogavano il log operativo. Le soglie
    # reali restano leggibili con `/autobet` e qui sopra nel report.
    logger.debug("auto_bet: strategia favoriti netti (EV_MIN=%.0f%%, ODDS "
                 "%.2f-%.2f, edge >= +%.0fpp, adaptive Kelly)",
                 EV_MIN * 100, ODDS_MIN, ODDS_MAX, MARKET_EDGE_MIN * 100)
    try:
        import multi_market as _mm
        logger.debug("auto_bet: corsie multi-mercato -> %s (AH live, OU "
                     "shadow di default; ENABLE_LIVE_AH=%s, ENABLE_LIVE_OU=%s)",
                     ", ".join(_mm.live_markets()) or "nessuna",
                     _mm.ENABLE_LIVE_AH, _mm.ENABLE_LIVE_OU)
    except Exception:
        pass

    # --- FASE 1: costruisci i candidati (guardie + stake, senza salvare) ---
    candidates: list[dict] = []
    # Corsia multi-mercato (19/09/2026): AH con ordini reali, OU in shadow.
    # Gli stessi guardrail valgono per tutti i pick (T-60, stop-loss, cap,
    # feed, dedup): la differenza tra le corsie e' solo l'INTERRUTTORE live.
    # Corsia TOP-DOWN (25/09, solo LIVE): quando il gate oracolo governa il
    # denaro reale, il board 1X2 non e' pre-filtrato dalla fascia bottom-up
    # (le righe scartate dal filtro quote restano nel ledger e diventano
    # candidati: il prezzo lo giudica Pinnacle, non la fascia 1.30-1.80).
    # In SIM il board resta quello storico (era del ledger intatta).
    board = _today_value_picks() + _multi_market_picks()
    if mode == "live" and TOP_DOWN_EV and TOP_DOWN_BYPASS:
        board = board + _top_down_picks()
    # Corsia eSports (30/09/2026): SOLO LIVE. L'oracolo costa quota OddsPapi
    # (250 richieste/mese) e il suo unico scopo e' decidere il PREZZO di un
    # ordine reale: in SIM si brucerebbe la quota per del paper trading.
    if mode == "live":
        board = board + _esports_picks()
        # Corsia TENNIS (30/09/2026): Denaro Reale, oracolo a 2 esiti. Con la
        # misura di oggi (EV massimo +0.72% su 104 lati) la soglia 2.5% non
        # produce ordini: la corsia e' ARMATA e spara al primo disallineamento.
        board = board + _tennis_picks()
    # DEDUP CROSS-CORSIA per (match_id, esito): la stessa riga del ledger puo'
    # arrivare da due corsie (value pick + corsia top-down) e il dedup sul
    # ledger (bet_exists_open) NON vede ancora l'ordine della prima: senza
    # questo filtro l'evento verrebbe ordinato DUE volte sul provider prima
    # che la riga `bets` esista (bug colto dal test live: 2 ordini su 1
    # evento). Vince la PRIMA occorrenza (ordine corsie: storico prima).
    _seen_pick: set[tuple] = set()
    _deduped: list[dict] = []
    for pick in board:
        _pk = (pick.get("match_id"), pick.get("esito_key"))
        if _pk in _seen_pick:
            logger.debug("auto_bet: pick duplicato cross-corsia %s (%s), "
                         "salto", pick.get("match_id"), pick.get("esito_key"))
            continue
        _seen_pick.add(_pk)
        _deduped.append(pick)
    board = _deduped
    for pick in board:
        if bet_exists_open(pick["match_id"], pick["esito_key"]):
            logger.debug("auto_bet: puntata gia' aperta per %s (%s), salto",
                         pick["match_id"], pick["esito_key"])
            continue

        # --- VALUTAZIONE TOP-DOWN (fase 2, 25/09): l'EV del segnale si
        # calcola contro l'ORACOLO Pinnacle (de-vigato, letto DALLE CACHE,
        # zero crediti) invece che contro le probabilita' del modello. Il
        # Poisson resta a monte: ha prodotto il candidato e popola il ledger,
        # ma non decide piu' il denaro. Un segnale senza oracolo (Pinnacle
        # assente/incompleto/stantio) NON si ordina: fail-closed — senza una
        # verita' di riferimento non c'e' ritardo da comprare.
        # SOLO sulla corsia LIVE: la corsia paper (SIM) mantiene la base
        # storica del segnale per non cambiare era al ledger che alimenta
        # ML/CLV (lezione 22/09: il campione si misura, non si riscrive);
        # l'oracolo governa il denaro reale.
        # La corsia eSports porta il PROPRIO oracolo (OddsPapi) e i suoi esiti
        # non sono un 1X2: applicare anche il gate Pinnacle la ucciderebbe con
        # `no_oracle` su ogni pick, perche' `load_oracle` legge le cache del
        # CALCIO dove gli eSports non esistono.
        # I mercati con ORACOLO PROPRIO (eSports `ML`, tennis `TENNIS`) non
        # passano dal gate 1X2 di Pinnacle: i loro esiti non sono un 1X2 e il
        # gate li ucciderebbe con `no_oracle`. Il tennis aggancia la cache per
        # NOMI giocatore con la forma a 2 esiti (gia' nella sua corsia).
        if (TOP_DOWN_EV and mode == "live"
                and str(pick.get("mercato") or "1X2").upper()
                not in ("ML", "TENNIS")):
            verdict = _top_down_eval(pick, league=pick.get("league"))
            if not verdict.get("ok"):
                logger.info("auto_bet: %s (%s) top-down SKIP [%s]: %s",
                            pick["match_id"], pick["esito_key"],
                            verdict.get("reason"), verdict.get("detail") or "")
                continue
            pick["p_true"] = verdict["p_true"]
            pick["top_down_ev"] = verdict["ev"]
            pick["true_odd"] = verdict["true_odd"]
            pick["required_price"] = verdict["required_price"]
            if not verdict["trigger"]:
                logger.info("auto_bet: %s (%s) @ %.2f EV top-down %+.2f%% < "
                            "%.1f%% (true odd %.3f, richiesto %.3f): no value",
                            pick["match_id"], pick["esito_key"],
                            float(pick.get("quota") or 0),
                            verdict["ev"] * 100.0,
                            verdict["ev_min"] * 100.0,
                            verdict["true_odd"], verdict["required_price"])
                continue
            logger.info("auto_bet: %s (%s) @ %.2f EV top-down %+.2f%% >= "
                        "%.1f%% (p_true %.3f, true odd %.3f): CANDIDATO",
                        pick["match_id"], pick["esito_key"],
                        float(pick.get("quota") or 0),
                        verdict["ev"] * 100.0, verdict["ev_min"] * 100.0,
                        verdict["p_true"], verdict["true_odd"])

        # SIM: quota del segnale, nessun catalogo.
        if _too_close_to_start(pick.get("commence")):
            logger.info("auto_bet: %s vs %s a meno di %d min dall'inizio, salto",
                        pick["home"], pick["away"], MIN_MINUTES_TO_START)
            continue
        price = float(pick["quota"] or 0)
        if price <= 1.0:
            logger.info("auto_bet: quota segnale non valida per %s, salto",
                        pick["match_id"])
            continue

        # --- STRATEGIA T-60 (17/09): la decisione esecutiva viene presa SOLO
        # nella finestra T-60..T-50 minuti prima del fischio d'inizio. Fuori
        # finestra il palinsesto resta SCANSIONATO e classificato (il ledger
        # e la catena shadow misurano tutto), ma nessun ordine parte: e' il
        # controllo del timing richiesto dal proprietario.
        if T60_EXECUTION_ONLY:
            _tw = t60_window(_parse_iso_utc(pick.get("commence")))
            if _tw != "within":
                logger.info("auto_bet: %s (%s) fuori finestra T-60 (%s): solo "
                            "scansione, nessun ordine", pick["match_id"],
                            pick["esito_key"], _tw)
                continue

        # Timing filter: piazza solo nel momento ottimale (0.5-24h prima)
        timing = get_optimal_timing(pick.get("commence"))
        if not timing["optimal"]:
            logger.info("auto_bet: %s (%s) timing non ottimale: %s, salto",
                        pick["match_id"], pick["esito_key"],
                        timing["reason"])
            continue

        # Flat-stake (09/09, Calcio 1X2): importo FISSO per ogni segnale
        # value/strong_value invece del Kelly dinamico. Il rispetto dei cap
        # (correlazione 30% + esposizione totale 40% del giorno) e del
        # minimo ordine SX avviene a UNITA' INTERE in FASE 2
        # (apply_flat_budget). Identico per SIM e live.
        if STAKE_MODE == "flat":
            pick_stake = normalize_stake(FLAT_STAKE_EUR)
            if pick_stake <= 0:
                logger.info("auto_bet: flat stake €%.2f sotto il minimo per "
                            "%s, salto", FLAT_STAKE_EUR, pick["match_id"])
                continue
            logger.info("auto_bet: stake flat €%.2f per %s (%s)",
                        pick_stake, pick["match_id"], pick["esito_key"])
        # Adaptive staking: stake dinamico (identico per SIM e live). Kelly
        # frazionato (0.05-0.40 via env KELLY_MIN/MAX_FRACTION) con drawdown
        # protection e confidence weighting (market_edge/status). Il cap per
        # singola bet (STAKE_CAP_PCT 1%/2%) e il CAP SEVERO sono applicati a
        # valle: il floor dell'exchange non deve MAI alzare lo stake sopra il
        # cap (in quel caso l'ordine viene saltato).
        elif _adaptive:
            as_result = adaptive_stake(
                bankroll=_bankroll,
                prob=(pick.get("best_ev", 0.0) + 1.0 / price) if price > 0 else 0.5,
                odds=price, market_edge=pick.get("market_edge"),
                status=pick.get("status", "value"),
                peak_bankroll=_peak,
                # CLV storico: conferma dell'edge -> stake piu' alto se
                # stiamo battendo la closing line (wiring del segnale CLV).
                has_clv_positive=(_avg_clv > 0.0))
            pick_stake = as_result["stake"]
            # Odds movement (sharp money): se il pick porta un movimento di
            # quota <= -5% lo stake sale del 20% (resta dentro i risk cap).
            odds_move = float(pick.get("odds_movement", 0.0) or 0.0)
            if pick_stake > 0 and odds_move <= MOVEMENT_THRESHOLD:
                pick_stake *= MOVEMENT_BONUS_MULTIPLIER
                logger.info("auto_bet: %s odds MOVEMENT %.1f%% (sharp money) "
                            "+20%% stake", pick["match_id"], odds_move * 100)
            # Ponderazione dinamica per campionato (26/09/2026, default OFF):
            # se la lega ha CLV sistematicamente negativo nella finestra
            # (30gg) il moltiplicatore di Kelly viene RIDOTTO. Con l'env
            # spenta ritorna 1.0 e il percorso resta identico al 22/09.
            _lm = _league_multiplier(pick.get("league"))
            if _lm < 1.0:
                _before = pick_stake
                pick_stake *= _lm
                logger.info("auto_bet: %s lega '%s' CLV negativo -> stake "
                            "x%.2f (€%.2f -> €%.2f)", pick["match_id"],
                            pick.get("league") or "?", _lm, _before, pick_stake)
            if pick_stake <= 0:
                logger.info("auto_bet: stake adaptive = 0 per %s (EV negativo), salto",
                            pick["match_id"])
                continue
            logger.info("auto_bet: stake adaptive €%.2f (EV=%.3f, "
                        "timing=%.1fh) per %s (%s)",
                        pick_stake, float(pick.get("best_ev", 0.0) or 0.0),
                        timing["hours_before"], pick["match_id"],
                        as_result["reason"])
        else:
            pick_stake = normalize_stake(stake_eur_default)
        if pick_stake <= 0:
            logger.info("auto_bet: stake %.2f sotto il minimo per %s, salto",
                        pick_stake, pick["match_id"])
            continue

        # LIVE: mai oltre i fondi LIBERI del wallet (_spendable = disponibile;
        # l'equity usata dal Kelly include anche quelli gia' in gioco, che non
        # si possono spendere due volte).
        # CAP SEVERO: se lo stake cappato e' sotto il minimo ordine
        # dell'exchange, il floor NON lo alza (sforerebbe il cap): l'ordine
        # viene saltato, a meno che il cap severo sia disattivato.
        if mode == "live":
            # --- STAKE FISSO (direttiva 28/09/2026): la size dell'ordine REALE
            # e' un IMPORTO FISSO (default 1.50 USDC), non piu' il Kelly.
            # `order_stake` e' l'unico punto di verita' (tetto per-ordine
            # inviolabile + vincolo di cassa): con fondi liberi insufficienti
            # restituisce 0 e l'ordine viene saltato — mai un importo diverso
            # dalla direttiva (fail-closed).
            pick_stake = order_stake(pick_stake, _spendable)
            if pick_stake < MIN_STAKE_EUR:
                if fixed_stake_active():
                    logger.warning("auto_bet: STAKE FISSO %.2f non sostenibile "
                                   "per %s (fondi liberi %.2f < minimo ordine "
                                   "%.2f): ordine saltato (fail-closed)",
                                   fixed_order_stake(), pick["match_id"],
                                   _spendable, MIN_STAKE_EUR)
                    continue
                if STAKE_CAP_HARD:
                    logger.warning("auto_bet: %s (%s)",
                                   hard_cap_skip_message(pick_stake, _bankroll),
                                   pick["match_id"])
                    continue
                pick_stake = MIN_STAKE_EUR
            pick_stake = round(pick_stake, 2)

        # Odds movement detection (bonus signal for stake sizing)
        odds_movement_val = pick.get("odds_movement", 0.0)

        candidates.append({
            **pick, "price": price, "stake": pick_stake,
            "odds_movement": odds_movement_val,
        })

    # --- CORSAIA CHIEF (27/09/2026, `CHIEF_EXECUTION=live`): i piani
    # approvati dalla catena piramidale entrano nella STESSA coda di
    # esecuzione. Da qui in avanti passano per gli stessi guardrail della
    # corsia storica: gate di mercato, cap di correlazione ed esposizione
    # totale, tetto per-ordine, recinto d'esposizione aperta, liquidita' e
    # ledger in `_live_fill`. Nessun canale di denaro parallelo.
    if mode == "live":
        for chief_cand in _chief_live_candidates(bankroll=_bankroll):
            if bet_exists_open(chief_cand["match_id"],
                               chief_cand["esito_key"]):
                logger.info("auto_bet: piano chief %s (%s) gia' coperto da "
                            "una puntata aperta, scartato",
                            chief_cand["match_id"],
                            chief_cand["esito_key"])
                continue
            candidates.append(chief_cand)

    # --- GATE DI MERCATO: refresh forzato del gateway (SX primaria) e verifica
    # --- PRIMA di qualunque ordine. Blocco fail-closed: senza un feed fresco,
    # --- conforme al contratto e validato il giro si ferma qui — nessun ordine,
    # --- nessuna riga sul ledger (vale anche per SIM: le puntate simulate
    # --- alimentano ML/CLV, un mercato non verificato le inquinerebbe).
    # --- Ordine delle autorita': kill switch e stop-loss hanno gia' risposto
    # --- sopra, quindi un problema tecnico di dati non li scavalca mai.
    if candidates:
        allowed, gate_reason, identity = _market_feed_gate(
            request_id=f"auto_bet-{mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M}")
        _last_market_gate.update({
            "blocked": not allowed, "reason": gate_reason.split(":", 1)[0],
            "detail": gate_reason,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            **identity,
        })
        if not allowed:
            logger.error("auto_bet: PUNTATE BLOCCATE dal gate di mercato — %s "
                         "(gateway %s, sorgente %s, feed validato %s)",
                         gate_reason, identity.get("gateway_id"),
                         identity.get("source"), identity.get("validated"))
            return []
        logger.info("auto_bet: feed di mercato ok — %s", gate_reason)

    # --- FASE 2: risk capping (correlazione + esposizione totale) ---
    # Calcola esposizione corrente
    _exposure = calculate_exposure(candidates, _bankroll)
    if _exposure.get("correlation_risk"):
        logger.warning("auto_bet: CORRELATION RISK! Max league exposure "
                       "%.1f%% > 30%% — riduci stake",
                       _exposure["max_league_pct"] * 100)
    if not _exposure.get("total_pct_ok"):
        logger.warning("auto_bet: Total exposure %.1f%% > 40%% — "
                       "cap ridotto", _exposure["total_pct"] * 100)
    # Il cap TOTALE e' giornaliero: sottrae l'esposizione gia' piazzata nei
    # giri precedenti (puntate aperte nelle ultime 24h), poi scarta i
    # candidati azzerati dal cap e (in LIVE) riapplica il floor exchange.
    if STAKE_MODE == "flat":
        # Flat: cap a unita' INTERE (le frazioni non sono piazzabili: il
        # minimo ordine SX Bet e' 1 USDC). Gli esuberi per EV vengono
        # azzerati e filtrati qui sotto.
        candidates = apply_flat_budget(
            candidates, _bankroll,
            already_placed=_today_placed_stake())
    else:
        candidates = apply_correlation_cap(candidates, _bankroll)
        candidates = apply_total_exposure_cap(
            candidates, _bankroll,
            already_placed=_today_placed_stake())
    candidates = [c for c in candidates if c.get("stake", 0) > 0]
    if mode == "live":
        kept = []
        _fixed = fixed_order_stake()
        for c in candidates:
            raw_stake = float(c["stake"])
            if _fixed > 0 and raw_stake < _fixed:
                # Un cap di portafoglio (correlazione/esposizione) ha ridotto lo
                # stake sotto l'importo fisso: non si rialza a 1.50 (sforerebbe
                # il cap) e non si scende sotto la direttiva -> ordine saltato
                # (fail-closed). I cap decidono SE, l'importo fisso decide QUANTO.
                logger.info("auto_bet: %s (%s) stake %.2f < importo fisso %.2f "
                            "dopo i cap di portafoglio: salto",
                            c.get("match_id"), c.get("esito_key"),
                            raw_stake, _fixed)
                continue
            stake = order_stake(raw_stake, _spendable)
            if stake < MIN_STAKE_EUR:
                if _fixed > 0:
                    logger.warning("auto_bet: STAKE FISSO %.2f non sostenibile "
                                   "per %s (fondi liberi %.2f): salto "
                                   "(fail-closed)", _fixed, c.get("match_id"),
                                   _spendable)
                    continue
                if STAKE_CAP_HARD:
                    # I risk cap (correlazione/esposizione) hanno ridotto lo
                    # stake sotto il minimo ordine: si salta, mai alzarlo al
                    # floor (sforerebbe il cap per singola bet).
                    logger.warning("auto_bet: %s (%s)",
                                   hard_cap_skip_message(stake, _bankroll),
                                   c.get("match_id"))
                    continue
                stake = MIN_STAKE_EUR
            c["stake"] = stake
            kept.append(c)
        candidates = kept

    # --- FASE 3: esegui e registra (LIVE via execution_engine oppure SIM) ---
    from tracker import save_bet
    placed: list[dict] = []
    dry_run_blocked = 0
    open_exposure_skipped = 0
    for cand in candidates:
        pick_stake = cand["stake"]
        price = cand["price"]
        if cand.get("corr_cap"):
            logger.info("auto_bet: stake ridotto da correlation cap per %s "
                        "(%s): €%.2f", cand["match_id"],
                        cand.get("corr_group", ""), pick_stake)
        if cand.get("total_cap"):
            logger.info("auto_bet: stake ridotto da cap esposizione totale per "
                        "%s (%s): €%.2f", cand["match_id"],
                        cand.get("total_cap_group", ""), pick_stake)

        # --- DRY-RUN (fase 2, 25/09): il candidato ha superato TUTTI i gate
        # (top-down EV, timing, cap, feed, liquidita'): qui partirebbe
        # l'ordine. Si LOGGA quello che sarebbe partito e si passa al
        # prossimo: NESSUN POST all'exchange, NESSUNA riga sul ledger (un
        # ordine non piazzato non deve sembrare piazzato ne' a ledger ne'
        # nei riepiloghi). E' l'intercettazione richiesta dal proprietario
        # per misurare il flusso della strategia senza muovere denaro.
        if DRY_RUN:
            dry_run_blocked += 1
            logger.warning("auto_bet: DRY-RUN — ordine INTERCETTATO %s vs %s "
                           "(%s @ %.2f, stake %.2f USDC, EV top-down %+.2f%%, "
                           "mode=%s): nessun POST a SX, nessuna riga sul "
                           "ledger", cand.get("home"), cand.get("away"),
                           cand.get("esito_key"), price, pick_stake,
                           float(cand.get("top_down_ev") or 0.0) * 100.0,
                           mode)
            continue

        if mode == "live":
            # Recinto d'esposizione: nessun NUOVO ordine reale finche' lo
            # stake aperto non torna sotto il tetto E il nuovo ordine non
            # farebbe superare il cap (PROIEZIONE: aperto + questo stake).
            # La lettura e' fresca per ogni candidato: piu' ordini nella
            # stessa tornata non possono sfondare il 40% sommandosi.
            _allow = exposure_allows(_bankroll, pick_stake)
            if _exposure_blocked or not _allow.get("allowed"):
                open_exposure_skipped += 1
                logger.info("auto_bet: %s (%s) sospeso dal recinto "
                            "esposizione aperta (%.2f/%.2f USDC, con questo "
                            "ordine %.2f, %s aperti): nessun ordine reale, "
                            "telemetria invariata",
                            cand.get("match_id"), cand.get("esito_key"),
                            _allow.get("open_stake"), _allow.get("cap"),
                            _allow.get("projected"), _allow.get("count"))
                continue
            filled = _live_fill(cand, pick_stake, price)
            if filled is None:
                # Saltata (mercato assente/ambiguo, prezzo sotto il floor EV,
                # errore di rete): nessun ordine, nessuna riga sul ledger.
                continue
            if not filled.get("ok"):
                # Ordine rifiutato/non riempito dall'exchange: niente riga
                # (un FAILED sul ledger verrebbe saldato come perdita reale).
                logger.warning("auto_bet: ordine live non piazzato per %s "
                               "(%s @ %.2f): %s", cand["match_id"],
                               cand["esito_key"], price,
                               filled.get("error") or filled.get("status"))
                continue
            matched_price = float(filled["price"] or price)
            matched_stake = float(filled["stake"] or pick_stake)
            # Ultima barriera prima della scrittura: una riga mode='live' E'
            # la prova che un ordine esiste sull'exchange. Senza bet_id non
            # si scrive nulla (il ledger non deve contenere un "successo"
            # che sulla piattaforma non esiste).
            if not filled.get("bet_id"):
                logger.error("auto_bet: ordine live per %s (%s) senza "
                             "bet_id: riga LIVE NON scritta sul ledger",
                             cand["match_id"], cand["esito_key"])
                continue
            record = {**cand, "market_id": filled["market_id"],
                      "selection_id": filled["selection_id"],
                      "status": filled.get("status") or "FILLED_UNCONFIRMED",
                      "bet_id": filled.get("bet_id"), "mode": "live",
                      "price": matched_price, "stake": matched_stake}
            placed.append(record)
            try:
                save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                         esito=cand["esito_key"],
                         market_id=filled["market_id"],
                         selection_id=filled["selection_id"],
                         price=matched_price, stake=matched_stake,
                         mode="live",
                         status=filled.get("status") or "FILLED_UNCONFIRMED",
                         bet_id=filled.get("bet_id"))
            except Exception as e:
                logger.warning("auto_bet: salvataggio live %s: %s",
                               cand["match_id"], e)
            continue

        # SIM (default): paper trading con la quota del segnale.
        record = {**cand, "market_id": None, "selection_id": None,
                  "status": "SUCCESS", "bet_id": None, "mode": "sim"}
        placed.append(record)
        try:
            save_bet(match_id=cand["match_id"], mercato=cand["mercato"],
                     esito=cand["esito_key"], market_id=None, selection_id=None,
                     price=price, stake=pick_stake, mode="sim", status="SUCCESS")
        except Exception as e:
            logger.warning("auto_bet: salvataggio sim %s: %s", cand["match_id"], e)

    _shadow_run(mode=mode, bankroll=_bankroll, placed=len(placed))
    # Heartbeat del giro: UNA riga per ciclo anche a giro vuoto (21/09/2026).
    # E' cio' che permette di vedere dal log che il job gira senza dover
    # leggere la configurazione ripetuta.
    if placed:
        logger.info("auto_bet: %d puntate piazzate (%s)", len(placed), mode)
    elif open_exposure_skipped:
        logger.warning("auto_bet: %d candidati sospesi dal recinto "
                       "esposizione aperta (%.2f/%s USDC in gioco): nessun "
                       "ordine reale, telemetria e shadow attivi",
                       open_exposure_skipped,
                       exposure_state.get("open_stake") or 0.0,
                       exposure_state.get("cap"))
    elif DRY_RUN and dry_run_blocked:
        logger.warning("auto_bet: DRY-RUN (%s) — %d candidati hanno superato "
                       "tutti i gate e sono stati INTERCETTATI prima "
                       "dell'ordine (dettagli nei log qui sopra)",
                       mode, dry_run_blocked)
    else:
        logger.info("auto_bet: nessuna puntata (%s) — 0 candidati giocabili",
                    mode)
    return placed


def _chief_live_candidates(*, bankroll: float, now=None) -> list[dict]:
    """Candidati REALI dalla catena piramidale (Chief Orchestrator).

    Non esiste un secondo canale di denaro: i piani approvati dal Finance
    Agent entrano nella STESSA coda di esecuzione della corsia storica e
    quindi passano per gli stessi guardrail (finestra T-60, timing, dedup
    `bet_exists_open`, quota sensa, gate di mercato, cap di correlazione ed
    esposizione, liquidita' in `_live_fill`, tetto per-ordine, recinto
    d'esposizione aperta). Cosa cambia rispetto alla corsia storica: qui lo
    STAKE e' quello deciso dalla Finanza (Kelly, cap tier/lega/risk,
    cap severo), non ricalcolato dalla corsia.

    Gate applicati qui, perche' nascono come piano e non come segnale grezzo:
      - verdetto `approve` + stake eseguibile (l'Engine Agent li richiede a sua
        volta, ma il Capo filtera: difesa in profondita');
      - quota nella fascia della strategia (1.30-1.80): un piano non puo'
        superarla perche' l'oracolo e' de-vigato altrove;
      - T-60 e timing: la finestra esecutiva e' un vincolo di denaro, non di
        segnaletica.

    Fail-safe totale: un errore della catena restituisce lista vuota e non
    interrompe mai il giro. Con `CHIEF_EXECUTION` != "live" non fa nulla.
    """
    if not chief_execution_enabled():
        return []
    out: list[dict] = []
    try:
        from chief_orchestrator import ChiefOrchestrator
        from decision.commands import CommandKind
        from value_filter import get_optimal_timing, ODDS_MIN, ODDS_MAX

        chief = ChiefOrchestrator()
        # La Finanza del ciclo lavora sul bankroll REALE e in modalita' live:
        # e' l'unica differenza rispetto al giro shadow (stessa-era).
        chief.finance.bankroll = float(bankroll or 0.0)
        chief.finance.mode = "live"
        # 1+2. Dati (feed di mercato) e Strategia (tier giocabili): stessi
        # agenti del Capo, quindi stesso feed validato e stesso filtro.
        market = chief.data.process(now=now)
        if not market.validated:
            logger.warning("auto_bet: ciclo chief live fermato dal gate di "
                           "mercato (%s): nessun ordine",
                           market.gate.reason.value)
            return []
        strategy = chief.strategy.process(market.signals)
        # 3. Finanza: un piano per segnale.
        finance = chief.finance.process_many(strategy.signals, now=now)
        for plan in finance.plans:
            if plan.record.risk.verdict != "approve" or not plan.places_order:
                continue
            stake_dec = plan.record.stake
            if stake_dec is None or not bool(getattr(stake_dec, "executable",
                                                     False)):
                continue
            order_cmd = plan.of_kind(CommandKind.PLACE_ORDER)[0]
            payload = dict(order_cmd.payload)
            price = float(payload.get("price") or 0.0)
            if not (ODDS_MIN <= price <= ODDS_MAX):
                logger.info("auto_bet: piano chief %s (%s) quota %.2f fuori "
                            "fascia %.2f-%.2f, scartato",
                            payload.get("match_id"), payload.get("outcome"),
                            price, ODDS_MIN, ODDS_MAX)
                continue
            kickoff = payload.get("kickoff")
            if T60_EXECUTION_ONLY and \
                    t60_window(_parse_iso_utc(kickoff)) != "within":
                logger.info("auto_bet: piano chief %s (%s) fuori finestra "
                            "T-60 (%s): solo scansione",
                            payload.get("match_id"), payload.get("outcome"),
                            t60_window(_parse_iso_utc(kickoff)))
                continue
            if not get_optimal_timing(kickoff)["optimal"]:
                logger.info("auto_bet: piano chief %s (%s) timing non "
                            "ottimale, scartato", payload.get("match_id"),
                            payload.get("outcome"))
                continue
            stake = order_stake(float(payload.get("stake") or 0.0))
            if stake <= 0:
                continue
            out.append({
                "match_id": payload.get("match_id"),
                "mercato": payload.get("market") or "1X2",
                "esito_key": payload.get("outcome"),
                "home": payload.get("home") or "",
                "away": payload.get("away") or "",
                "commence": kickoff,
                "quota": price,
                "league": payload.get("league") or "",
                "price": price,
                "stake": stake,
                "lane": "chief",
                "signal_id": getattr(order_cmd, "signal_id", "") or "",
                "record_id": getattr(order_cmd, "record_id", "") or "",
            })
        if out:
            logger.info("auto_bet: %d piani chief approvati entrano nella "
                        "coda di esecuzione (stake %s)", len(out),
                        ", ".join("%.2f" % c["stake"] for c in out))
    except Exception as exc:
        logger.warning("auto_bet: ciclo chief live saltato (%s)", exc)
        return []
    return out


def _shadow_run(*, mode: str, bankroll: float, placed: int = 0) -> dict | None:
    """Shadow mode (15/09/2026): la catena Command valuta gli stessi segnali.

    Emette e REGISTRA i comandi che la catena nuova produrrebbe, senza eseguire
    nulla: nessun ordine, nessuna notifica, nessuna riga sul ledger `decisions`
    (il giro gira ogni 60s: il registro shadow e' deduplicato). Serve al
    confronto misurato prima di sostituire il percorso attuale (passo 3).

    Fail-safe: qualunque errore viene loggato e non tocca il giro puntate.
    Si spegne con `DECISION_SHADOW=0`.

    **Feed di mercato (15/09/2026)**: la catena forza il refresh del gateway
    SX (`decision/feeds.py`) prima di ogni valutazione di rischio e resta
    ferma finche' il feed non e' fresco, conforme al contratto e validato. E'
    una lettura PUBBLICA dell'exchange: zero crediti the-odds-api, nessun
    ordine. Si spegne con `DECISION_FEED_ENABLED=0` (la catena valuta allora
    senza il gate di mercato, senza toccare la rete).
    """
    try:
        from decision.shadow import run_shadow, shadow_enabled
        if not shadow_enabled():
            return None
        from decision.middleware import Observability

        obs = Observability(component="auto_bet.shadow")
        summary = run_shadow(bankroll=bankroll, mode=mode, observability=obs,
                             request_id=f"auto_bet-{mode}")
        if summary.get("blocked"):
            logger.info("auto_bet shadow: catena ferma dal fail-fast (%s)",
                        (summary["blocked"] or {}).get("name"))
            return summary
        market = summary.get("market") or {}
        if market:
            logger.info("auto_bet shadow: feed gateway=%s sorgente=%s schema=%s "
                        "config=%s request=%s | %s quote (validato=%s)",
                        market.get("gateway_id"), market.get("source"),
                        market.get("schema_version"), market.get("config_hash"),
                        market.get("request_id"), market.get("accepted"),
                        market.get("verified"))
        if summary.get("market_blocked"):
            logger.warning("auto_bet shadow: gate di mercato -> %s "
                           "(nessuna valutazione di rischio)", summary["market_blocked"])
        # A INFO solo quando c'e' qualcosa da dire (un segnale valutato o un
        # comando emesso): a giro vuoto il riepilogo shadow e' una riga di
        # rumore identica ogni 60s (21/09/2026).
        _evaluated = summary.get("evaluated", 0)
        _commands = sum((summary.get("by_command") or {}).values())
        logger.log(logging.INFO if (_evaluated or _commands) else logging.DEBUG,
                   "auto_bet shadow: %d segnali valutati %s | %d comandi "
                   "registrati (0 ordini reali, esecuzione invariata) | %d "
                   "revisioni in coda per l'umano",
                   _evaluated, summary.get("by_verdict") or {}, _commands,
                   summary.get("reviews_queued", 0))
        return summary
    except Exception as exc:
        logger.warning("auto_bet shadow: valutazione saltata (%s)", exc)
        return None
    finally:
        # FASE 2 (27/09/2026): il ciclo del Chief (piramide a 4 agenti) valuta
        # gli stessi segnali e registra il riepilogo su un JSONL dedicato.
        # NESSUN effetto reale (gateway shadow), zero crediti, fail-safe totale:
        # un errore e' una riga di log, non tocca il giro puntate. Si spegne
        # con `CHIEF_SHADOW_ENABLED=0`. Lettura: `python chief_shadow_wiring.py`.
        try:
            from chief_shadow_wiring import chief_shadow_enabled, run_chief_cycle_shadow
            if chief_shadow_enabled():
                chief_rec = run_chief_cycle_shadow(bankroll=bankroll, mode=mode)
                if chief_rec is not None:
                    logger.debug("chief shadow: ciclo registrato (ok=%s, finance=%s)",
                                 chief_rec.get("ok"), chief_rec.get("finance"))
        except Exception as exc:  # doppia cintura: mai rompere il giro
            logger.debug("chief shadow: hook non disponibile (%s)", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_today_bets()
    mode = res[0]['mode'] if res else 'nessuna'
    print(f"✅ {len(res)} puntate ({mode})")
    for p in res:
        print(f"• {p['home']} vs {p['away']} — {p['esito_key']} @ {p['price']:.2f} "
              f"(€{p['stake']:.2f}) [{p['status']}]")