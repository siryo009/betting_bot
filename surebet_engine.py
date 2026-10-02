"""surebet_engine.py — Scanner di arbitraggio (surebet) INDIPENDENTE dal bot Value Bet.

Modulo separato per il rilevamento di opportunita' di arbitraggio su mercati
a 2 esiti (h2h / moneyline / testa a testa) via The Odds API.

Vincoli architetturali (come da specifica):
- NON condivide stato, database o risorse col bot Value Bet: cache, log e
  loop sono PROPRI (data/surebet/), nessun import da tracker.py/bot.py.
- Solo The Odds API (scansione quote). L'esecuzione degli ordini NON è
  competenza di questo modulo: passa da execution_engine.py (aggregatore
  BetInAsia BLACK / MollyBet, dal 06/09).
- Sport: NBA (basketball_nba), MLB (baseball_mlb), Tennis (chiavi per torneo
  tennis_atp_*/tennis_wta_*, configurabili).
- Mercati: SOLO h2h con 2 esiti (moneyline/testa a testa).
- Confronto: bookmaker SOFT (Snai, GoldBet, altri EU) vs SHARP (Pinnacle)
  oppure soft-vs-soft. Trigger puramente matematico:
        (1/Quota_A) + (1/Quota_B) < 1
  con filtro anti-garbage SUREBET_MAX_ODDS (default 30): le coppie con
  quote fuori range (esiti impossibili/mercati illiquidi) sono scartate
  prima del trigger.
- Stake: allocazione esatta per bilanciare il profitto, basata sull'env
  SUREBET_BUDGET (es. 100).
- Consegna: Telegram con formato dedicato + INLINE KEYBOARD (deep linking:
  un bottone per esito "Piazza €X su Bookmaker" che apre l'URL diretto al
  palinsesto del bookmaker); funzione di delivery predisposta per un
  futuro invio JSON via webhook (n8n).

Uso (loop separato dal bot — crontab o processo dedicato):
    venv/bin/python surebet_engine.py            # scan singolo + notifica
    venv/bin/python surebet_engine.py --loop 300 # loop ogni 300s
    venv/bin/python surebet_engine.py --json     # solo output JSON (no notifica)
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from config import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env, con default)
# ---------------------------------------------------------------------------
SUREBET_DATA_DIR = Path(os.getenv("SUREBET_DATA_DIR", str(DATA_DIR / "surebet")))
CACHE_DIR = SUREBET_DATA_DIR / "cache"
LOG_FILE = SUREBET_DATA_DIR / "opportunities.jsonl"
# Heartbeat scritto a OGNI run (anche quando non ci sono opportunita'):
# prova tangibile su volume che il cron scatta davvero e con che frequenza
# (i log delle esecuzioni cron Railway non sempre sono esposti via CLI).
HEARTBEAT_FILE = SUREBET_DATA_DIR / "heartbeat.json"

# Crediti the-odds-api: budget CONSERVATIVO. La chiave e' CONDIVISA col
# bot Value Bet (piano free ~500/mese, il calendario value ne consuma
# ~407-460): ogni chiamata odds costa 1 credito per sport. Con TTL 6h e
# 2 sport core (NBA+MLB) si arriva a ~8 crediti/giorno (~240/mese) — gia'
# vicino al tetto; il tennis (chiavi per torneo) va abilitato SOLO per i
# tornei in corso via SUREBET_SPORTS. Sotto SUREBET_MIN_REMAINING lo
# scanner si ferma per non intaccare il budget del value bot.
ODDS_TTL = int(os.getenv("SUREBET_ODDS_TTL", "43200"))        # 12h per sport
SUREBET_MIN_REMAINING = int(os.getenv("SUREBET_MIN_REMAINING", "50"))

# Budget per il calcolo degli stake (importo totale da distribuire)
SUREBET_BUDGET = float(os.getenv("SUREBET_BUDGET", "100"))

# Margine minimo di arbitraggio: inverse_sum deve essere <= 1 - MIN_MARGIN
# (default 0.005 = servono almeno 0.5% di margine per segnalare; evita il
# rumore delle quote arrotondate a 2 decimali).
MIN_MARGIN = float(os.getenv("SUREBET_MIN_MARGIN", "0.005"))

# Filtro max-odds (anti garbage/mercati illiquidi): le coppie con una
# quota oltre questa soglia vengono scartate PRIMA del trigger matematico.
# Verificato sui dati reali 05/09: quote tipo "Arizona @85.00 vs @1.03"
# sono prezzi sporchi dell'API (esiti quasi impossibili o mercati senza
# liquidita'), non arbitraggi eseguibili: (1/85+1/1.03) < 1 scatta per
# puro artefatto aritmetico. Default 30 (su h2h 2 esiti una quota sana
# raramente supera 10-15; 30 e' gia' tollerante). Env: SUREBET_MAX_ODDS.
MAX_ODDS = float(os.getenv("SUREBET_MAX_ODDS", "30"))

# Chiavi sport The Odds API. Default credito-conservativo: SOLO i 2 sport
# core (NBA + MLB). Il tennis NON ha una chiave unica: sono chiavi per
# torneo (tennis_atp_*, tennis_wta_*), ognuna costa 1 credito per chiamata:
# abilitare solo i tornei IN CORSO via env, es.
#   SUREBET_SPORTS="basketball_nba,baseball_mlb,tennis_atp_us_open,tennis_wta_us_open"
SPORTS = [s.strip() for s in os.getenv(
    "SUREBET_SPORTS",
    "basketball_nba,baseball_mlb",
).split(",") if s.strip()]

# Bookmaker SHARP (benchmark di mercato). Default: Pinnacle.
SHARP_BOOKS = [b.strip().lower() for b in os.getenv(
    "SUREBET_SHARP_BOOKS", "Pinnacle").split(",") if b.strip()]

# Bookmaker SOFT (italiani/europei presenti su the-odds-api). Match per
# sottostringa normalizzata: "snai" copre "Snai"/"SNAI", "goldbet" copre
# "GoldBet", ecc. Estendibile via env SUREBET_SOFT_BOOKS.
SOFT_BOOKS = [b.strip().lower() for b in os.getenv(
    "SUREBET_SOFT_BOOKS",
    "Snai,GoldBet,Bet365,William Hill,Bwin,Unibet,Sisal,Eurobet,"
    "Betflag,Novibet,Stanleybet,888sport,Marathonbet,marathon bet,10bet,"
    "Betway,Paddy Power,Coral,betsson,betclic,tipico,winamax,1xbet,"
    "leovegas,nordic bet,gtbets,pmu,williamhill",
).split(",") if b.strip()]

# URL diretti al palinsesto per il deep linking (Inline Keyboard).
# Chiave = sottostringa normalizzata del nome bookmaker (stessa logica di
# SOFT_BOOKS): "winamax" copre "Winamax (DE)", "marathon" copre
# "Marathon Bet"/"Marathonbet". L'ordine conta (chiavi specifiche prima).
# NB: the-odds-api non espone URL evento per evento, quindi il link porta
# al palinsesto/sport del bookmaker: l'utente arriva a un tap dall'esito.
BOOKMAKER_LINKS: Dict[str, str] = {
    "snai": "https://www.snai.it/sport",
    "goldbet": "https://www.goldbet.it/sport",
    "bet365": "https://www.bet365.it/",
    "william hill": "https://sports.williamhill.it/",
    "williamhill": "https://sports.williamhill.it/",
    "bwin": "https://sports.bwin.it/",
    "unibet": "https://www.unibet.it/sport",
    "sisal": "https://www.sisal.it/scommesse",
    "eurobet": "https://www.eurobet.it/sport",
    "betflag": "https://www.betflag.it/sport",
    "novibet": "https://www.novibet.it/",
    "stanleybet": "https://www.stanleybet.it/",
    "888": "https://www.888sport.it/",
    "marathon": "https://www.marathonbet.it/",
    "10bet": "https://www.10bet.it/",
    "betway": "https://sports.betway.it/",
    "paddy power": "https://www.paddypower.com/",
    "coral": "https://sports.coral.co.uk/",
    "betsson": "https://www.betsson.com/",
    "betclic": "https://www.betclic.it/",
    "tipico": "https://www.tipico.it/",
    "winamax": "https://www.winamax.it/",
    "1xbet": "https://1xbet.it/",
    "leovegas": "https://www.leovegas.it/",
    "nordic": "https://www.nordicbet.com/",
    "gtbets": "https://www.gtbets.com/",
    "pmu": "https://www.pmu.fr/",
    "pinnacle": "https://www.pinnacle.com/",
}

# Webhook n8n (futuro): se impostato, il delivery invia anche il payload
# JSON a questo URL (es. istanza n8n esterna). Vuoto = solo Telegram.
N8N_WEBHOOK_URL = os.getenv("SUREBET_WEBHOOK_URL", "")

# Telegram: riusa token e chat ADMIN del bot, ma invia in modo INDIPENDENTE
# (POST diretto all'API Telegram, nessuno stato condiviso col bot).
TELEGRAM_BOT_TOKEN = os.getenv("QUOTAVERACE_BOT_TOKEN", "")
ADMIN_CHAT_IDS = [int(c.strip()) for c in os.getenv("ADMIN_CHAT_ID", "").split(",")
                  if c.strip().lstrip("-").isdigit()]

# ---------------------------------------------------------------------------
# Modello dati
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SurebetOpportunity:
    """Opportunita' di arbitraggio su un mercato h2h a 2 esiti."""
    timestamp: str
    sport_key: str
    evento: str                 # "Home Team vs Away Team" (o giocatori)
    commence_time: str
    esito_a: str
    esito_b: str
    bookmaker_a: str
    bookmaker_b: str
    quota_a: float
    quota_b: float
    inverse_sum: float
    margin: float               # inverse_sum - 1 (< 0 = arbitraggio)
    stake_a: float
    stake_b: float
    budget: float
    profit: float               # profitto netto garantito (€)
    roi_pct: float              # profit / budget * 100
    tipo: str                   # "soft-sharp" | "soft-soft" | "sharp-soft"

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SurebetOpportunity":
        return cls(**d)


# ---------------------------------------------------------------------------
# Logica core
# ---------------------------------------------------------------------------

def inverse_sum(quota_a: float, quota_b: float) -> Optional[float]:
    """Somma degli inversi: (1/A) + (1/B). None se quote invalide."""
    if not quota_a or not quota_b or quota_a <= 1.0 or quota_b <= 1.0:
        return None
    return 1.0 / quota_a + 1.0 / quota_b


def is_arbitrage(quota_a: float, quota_b: float,
                 min_margin: float = MIN_MARGIN) -> bool:
    """Trigger matematico: (1/A)+(1/B) < 1 - min_margin."""
    inv = inverse_sum(quota_a, quota_b)
    return inv is not None and inv <= 1.0 - min_margin


def is_sane_odds(quota: float, max_odds: float = MAX_ODDS) -> bool:
    """Filtro anti garbage: True se la quota e' entro il range ammesso.

    Scarta quote irrealistiche (> max_odds) tipiche di esiti quasi
    impossibili o mercati illiquidi: l'arbitraggio su quei prezzi e'
    un artefatto aritmetico, non eseguibile. Soglia via SUREBET_MAX_ODDS.
    """
    return 1.0 < quota <= max_odds


def compute_stakes(quota_a: float, quota_b: float,
                   budget: float = SUREBET_BUDGET) -> Optional[Tuple[float, float, float, float]]:
    """Allocazione esatta degli stake per bilanciare il profitto.

    Formula standard dell'arbitraggio: stake_i proporzionale all'inverso
    della quota, normalizzato sulla somma degli inversi.

    Ritorna (stake_a, stake_b, profit, roi_pct) oppure None se non arbitrabile.
    """
    inv = inverse_sum(quota_a, quota_b)
    if inv is None or inv >= 1.0:
        return None
    stake_a = budget * (1.0 / quota_a) / inv
    stake_b = budget * (1.0 / quota_b) / inv
    # Profitto identico su entrambi gli esiti (entro arrotondamento)
    profit_a = stake_a * quota_a - budget
    profit_b = stake_b * quota_b - budget
    profit = min(profit_a, profit_b)
    roi = profit / budget * 100.0
    return round(stake_a, 2), round(stake_b, 2), round(profit, 2), round(roi, 2)


def classify_bookmaker(title: str) -> str:
    """Classifica un bookmaker: 'sharp', 'soft' o 'other' (match normalizzato)."""
    t = (title or "").strip().lower()
    if any(s in t for s in SHARP_BOOKS):
        return "sharp"
    if any(s in t for s in SOFT_BOOKS):
        return "soft"
    return "other"


def _pair_type(book_a: str, book_b: str) -> Optional[str]:
    """Tipo di coppia ammessa: richiede ALMENO un bookmaker soft.

    L'arbitraggio tra due sharp (es. Pinnacle vs Pinnacle) e' impossibile o
    ineseguibile; la specifica chiede soft-vs-sharp oppure soft-vs-soft.
    """
    ca, cb = classify_bookmaker(book_a), classify_bookmaker(book_b)
    if ca == "soft" and cb == "sharp":
        return "soft-sharp"
    if ca == "sharp" and cb == "soft":
        return "sharp-soft"
    if ca == "soft" and cb == "soft":
        return "soft-soft"
    return None


def extract_h2h_outcomes(match: dict) -> Optional[Tuple[str, str, List[dict]]]:
    """Estrae le quote h2h (2 esiti) di un match the-odds-api.

    Il mercato h2h di NBA/MLB/tennis ha ESATTAMENTE 2 outcomes
    (home/away oppure giocatore A/B): qualsiasi altro numero di esiti
    viene scartato (la specifica e' a 2 esiti).

    Ritorna (esito_a, esito_b, quote_books) dove quote_books e' la lista
    di {bookmaker, esito, quota} per ogni bookmaker presente.
    """
    home = match.get("home_team") or ""
    away = match.get("away_team") or ""
    if not home or not away:
        return None
    books: List[dict] = []
    for bk in match.get("bookmakers", []) or []:
        title = bk.get("title") or ""
        for mkt in bk.get("markets", []) or []:
            if mkt.get("key") != "h2h":
                continue
            outcomes = mkt.get("outcomes", []) or []
            if len(outcomes) != 2:
                continue
            o1, o2 = outcomes[0], outcomes[1]
            for o in (o1, o2):
                name = (o.get("name") or "").strip()
                price = float(o.get("price") or 0)
                if not name or price <= 1.0:
                    continue
                # normalizza: per NBA/MLB il nome esatto e' la squadra;
                # per il tennis il nome del giocatore. Se un bookmaker
                # non allinea i nomi, il match viene comunque registrato.
                books.append({"bookmaker": title, "esito": name, "quota": price})
            break  # un solo mercato h2h per bookmaker
    if not books:
        return None
    return home, away, books


def scan_match(match: dict, sport_key: str) -> List[SurebetOpportunity]:
    """Scansiona un match per arbitraggi h2h a 2 esiti tra bookmaker.

    Per ogni COPPIA di bookmaker distinti (almeno uno soft) confronta la
    quota migliore per ciascun esito e applica il trigger matematico.
    Per ogni coppia tiene SOLO la combinazione a margine migliore.
    """
    parsed = extract_h2h_outcomes(match)
    if parsed is None:
        return []
    home, away, books = parsed

    # Migliore quota per (esito, bookmaker): se lo stesso bookmaker offre
    # lo stesso esito piu' volte, tiene la quota piu' alta.
    best: Dict[Tuple[str, str], float] = {}
    for b in books:
        key = (b["esito"], b["bookmaker"])
        if key not in best or b["quota"] > best[key]:
            best[key] = b["quota"]

    # Nomi esiti: usa i due nomi distinti trovati (ordine = come appaiono)
    esiti = sorted({e for e, _ in best})
    if len(esiti) != 2:
        return []

    opps: List[SurebetOpportunity] = []
    # considera la coppia (esiti[0], esiti[1])
    ea, eb = esiti[0], esiti[1]
    books_a = sorted([(bk, q) for (e, bk), q in best.items() if e == ea],
                     key=lambda x: -x[1])
    books_b = sorted([(bk, q) for (e, bk), q in best.items() if e == eb],
                     key=lambda x: -x[1])

    seen_pairs: set = set()
    for bka, qa in books_a:
        for bkb, qb in books_b:
            if bka == bkb:
                continue  # stesso bookmaker su entrambi gli esiti: non eseguibile
            pair = tuple(sorted((bka.lower(), bkb.lower())))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            ptype = _pair_type(bka, bkb)
            if ptype is None:
                continue
            # filtro max-odds: quote sporche/mercati illiquidi fuori range
            if not is_sane_odds(qa) or not is_sane_odds(qb):
                continue
            if not is_arbitrage(qa, qb):
                continue
            stakes = compute_stakes(qa, qb)
            if stakes is None:
                continue
            stake_a, stake_b, profit, roi = stakes
            inv = inverse_sum(qa, qb)
            opps.append(SurebetOpportunity(
                timestamp=datetime.now(timezone.utc).isoformat(),
                sport_key=sport_key,
                evento=f"{home} vs {away}",
                commence_time=match.get("commence_time", ""),
                esito_a=ea, esito_b=eb,
                bookmaker_a=bka, bookmaker_b=bkb,
                quota_a=qa, quota_b=qb,
                inverse_sum=round(inv, 5),
                margin=round(inv - 1.0, 5),
                stake_a=stake_a, stake_b=stake_b,
                budget=SUREBET_BUDGET, profit=profit, roi_pct=roi,
                tipo=ptype,
            ))

    opps.sort(key=lambda o: o.margin)  # margine piu' negativo prima
    return opps


# ---------------------------------------------------------------------------
# Fetch The Odds API (cache e crediti PROPRI)
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    exact = os.getenv(name)
    if exact is not None:
        return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name:
            return v.strip()
    return ""


def fetch_odds_sport(sport_key: str) -> List[dict]:
    """Quote h2h di uno sport da The Odds API, con cache TTL propria.

    Ritorna [] se chiave mancante, crediti insufficienti o errore rete.
    NON tocca la cache del bot Value Bet (CACHE_DIR e' data/surebet/cache).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"toa_{sport_key}.json"

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < ODDS_TTL:
                return data.get("payload", [])
        except Exception:
            pass

    key = _env("ODDS_API_KEY")
    if not key:
        logger.warning("surebet: ODDS_API_KEY mancante")
        return []
    try:
        # regions eu,uk: piu' bookmaker europei/UK a costo zero (la stessa
        # chiamata copre entrambe le regioni). Verificato 05/09 su NBA:
        # con solo "eu" non arrivano Pinnacle/Snai/GoldBet, ma si vedono
        # 1xBet, Betclic, Betsson, GTbets, LeoVegas, Marathon Bet, Nordic
        # Bet, PMU, Tipico, Unibet, Winamax, 888sport...
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={"apiKey": key, "regions": "eu,uk", "markets": "h2h",
                    "oddsFormat": "decimal"},
            timeout=30,
        )
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning("surebet the-odds-api bloccata (%s)", r.status_code)
            return []
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning("surebet the-odds-api %s: %s", sport_key, e)
        return []

    if remaining < SUREBET_MIN_REMAINING:
        logger.warning("surebet: crediti residui %s < %s, stop",
                       remaining, SUREBET_MIN_REMAINING)
        return []

    cache_file.write_text(json.dumps(
        {"ts": time.time(), "payload": payload, "remaining": remaining}))
    logger.info("surebet %s: %d match | crediti residui: %d",
                sport_key, len(payload), remaining)
    return payload


# ---------------------------------------------------------------------------
# Scanner orchestratore
# ---------------------------------------------------------------------------

def scan_all_sports(sports: Optional[List[str]] = None) -> List[SurebetOpportunity]:
    """Scansiona tutti gli sport configurati e ritorna le opportunita' valide."""
    sports = sports or SPORTS
    found: List[SurebetOpportunity] = []
    for sk in sports:
        payload = fetch_odds_sport(sk)
        for match in payload:
            found.extend(scan_match(match, sk))
    found.sort(key=lambda o: o.margin)
    return found


# ---------------------------------------------------------------------------
# Persistenza (log JSONL PROPRIO, non tocca il DB del bot)
# ---------------------------------------------------------------------------

def _seen_signature(opp: SurebetOpportunity) -> str:
    """Firma di dedup: evento + esiti + bookmaker + quote arrotondate."""
    return f"{opp.evento}|{opp.esito_a}|{opp.esito_b}|" \
           f"{opp.bookmaker_a}|{opp.bookmaker_b}|{opp.quota_a:.2f}|{opp.quota_b:.2f}"


def already_reported(sig: str, window_hours: float = 24.0) -> bool:
    """True se un'opportunita' identica e' gia' stata loggata di recente."""
    if not LOG_FILE.exists():
        return False
    cutoff = time.time() - window_hours * 3600
    try:
        for line in LOG_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("signature") == sig and row.get("ts", 0) > cutoff:
                return True
    except Exception:
        pass
    return False


def log_opportunity(opp: SurebetOpportunity) -> None:
    """Registra l'opportunita' nel log JSONL (dedup + storico)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = opp.to_dict()
    row["ts"] = time.time()
    row["signature"] = _seen_signature(opp)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Delivery: Telegram (formato dedicato) + webhook n8n predisposto
# ---------------------------------------------------------------------------

def bookmaker_url(title: str) -> Optional[str]:
    """URL diretto al palinsesto per un bookmaker supportato.

    Match per sottostringa normalizzata (stessa logica di SOFT_BOOKS):
    "Winamax (DE)" → URL Winamax, "Marathon Bet" → URL Marathonbet.
    Ritorna None per bookmaker non in tabella.
    """
    t = (title or "").strip().lower()
    for key, url in BOOKMAKER_LINKS.items():
        if key in t:
            return url
    return None


def build_inline_keyboard(opp: SurebetOpportunity) -> dict:
    """Inline Keyboard Telegram: un bottone cliccabile per esito.

    Deep linking: il testo del bottone contiene lo STAKE CALCOLATO
    ("Piazza €54.17 su Snai") e il tap apre l'URL diretto al palinsesto
    del bookmaker — guida l'esecuzione umana in un solo tap. Per i
    bookmaker fuori tabella, fallback su ricerca web (link comunque
    cliccabile, nessun bottone morto).

    Ritorna il dict `inline_keyboard` pronto per reply_markup Telegram.
    NB: il testo dei bottoni e' PLAIN TEXT (Telegram non applica HTML
    nei bottoni) quindi non va escaped.
    """
    rows: List[List[dict]] = []
    for esito, book, stake in (
        (opp.esito_a, opp.bookmaker_a, opp.stake_a),
        (opp.esito_b, opp.bookmaker_b, opp.stake_b),
    ):
        url = bookmaker_url(book)
        if not url:
            url = (f"https://www.google.com/search?q="
                   f"{quote(f'{book} scommesse palinsesto')}")
        rows.append([{
            "text": f"Piazza €{stake:.2f} su {book}",
            "url": url,
        }])
    return {"inline_keyboard": rows}


def format_telegram_alert(opp: SurebetOpportunity) -> str:
    """Formato dedicato per il segnale surebet su Telegram.

    Usa parse_mode HTML (non Markdown legacy: gli asterischi spaiati nei
    nomi reali fanno fallire il parse con 400). I campi dati sono escaped.
    """
    ev = html.escape(opp.evento)
    ea = html.escape(opp.esito_a)
    eb = html.escape(opp.esito_b)
    ba = html.escape(opp.bookmaker_a)
    bb = html.escape(opp.bookmaker_b)
    sk = html.escape(opp.sport_key)
    lines = [
        f"⚡ <b>ARBITRAGGIO CONFERMATO</b> ⚡",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"💰 <b>ROI netto: +{opp.roi_pct:.2f}%</b>",
        f"   Profitto: €{opp.profit:.2f} su €{opp.budget:.0f}",
        f"",
        f"🏟 <b>{ev}</b>",
        f"   Sport: {sk}",
        f"   Inizio: {opp.commence_time[:16]} UTC",
        f"   Mercato: 2 esiti (h2h)",
        f"",
        f"📊 <b>Quote e stake (SUREBET_BUDGET=€{opp.budget:.0f}):</b>",
        f"   • {ea} @ {opp.quota_a:.2f} → "
        f"<b>€{opp.stake_a:.2f}</b> su <b>{ba}</b>",
        f"   • {eb} @ {opp.quota_b:.2f} → "
        f"<b>€{opp.stake_b:.2f}</b> su <b>{bb}</b>",
        f"",
        f"🧮 Somma inversi: {opp.inverse_sum:.4f} (&lt; 1 ✅)",
        f"🔀 Coppia: {opp.tipo}",
        f"",
        f"⚠️ <b>NOTA</b>: profitto teorico garantito SOLO se entrambe le "
        f"quote restano disponibili al momento della puntata. Verifica la "
        f"disponibilita' effettiva. Gioca responsabilmente.",
    ]
    return "\n".join(lines)


def build_json_payload(opp: SurebetOpportunity) -> dict:
    """Payload JSON strutturato per il webhook esterno (n8n).

    Predisposto per il futuro invio verso n8n: stesso contenuto del
    segnale Telegram ma in formato macchina (evento, quote, stake, ROI).
    """
    return {
        "type": "surebet",
        "timestamp": opp.timestamp,
        "event": opp.evento,
        "sport": opp.sport_key,
        "commence_time": opp.commence_time,
        "market": "h2h",
        "outcomes": [
            {"esito": opp.esito_a, "bookmaker": opp.bookmaker_a,
             "quota": opp.quota_a, "stake": opp.stake_a},
            {"esito": opp.esito_b, "bookmaker": opp.bookmaker_b,
             "quota": opp.quota_b, "stake": opp.stake_b},
        ],
        "inverse_sum": opp.inverse_sum,
        "margin": opp.margin,
        "budget": opp.budget,
        "profit": opp.profit,
        "roi_pct": opp.roi_pct,
        "pair_type": opp.tipo,
        "links": [
            {"bookmaker": opp.bookmaker_a, "url": bookmaker_url(opp.bookmaker_a)},
            {"bookmaker": opp.bookmaker_b, "url": bookmaker_url(opp.bookmaker_b)},
        ],
    }


def _send_telegram(opp: SurebetOpportunity) -> bool:
    """Invia il segnale su Telegram in modo INDIPENDENTE (POST diretto
    all'API Telegram, nessuno stato condiviso col bot in polling)."""
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_IDS:
        logger.warning("surebet: token o chat Telegram mancanti, notifica saltata")
        return False
    text = format_telegram_alert(opp)
    ok = True
    for chat_id in ADMIN_CHAT_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "reply_markup": build_inline_keyboard(opp)},
                timeout=20,
            )
            if r.status_code != 200:
                logger.warning("surebet telegram %s: HTTP %s", chat_id, r.status_code)
                ok = False
        except Exception as e:
            logger.warning("surebet telegram %s: %s", chat_id, e)
            ok = False
    return ok


def _send_webhook(opp: SurebetOpportunity) -> bool:
    """Invio JSON verso n8n (futuro): attivo solo se SUREBET_WEBHOOK_URL
    e' impostato. Il payload e' build_json_payload(opp)."""
    if not N8N_WEBHOOK_URL:
        return False
    try:
        r = requests.post(N8N_WEBHOOK_URL, json=build_json_payload(opp),
                          timeout=15)
        if r.status_code >= 400:
            logger.warning("surebet webhook n8n: HTTP %s", r.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("surebet webhook n8n: %s", e)
        return False


def deliver_alert(opp: SurebetOpportunity) -> Tuple[bool, bool]:
    """Consegna del segnale: Telegram + webhook (se configurato).

    Predisposto per l'estensione: aggiungere un nuovo canale = una nuova
    funzione _send_* chiamata qui (n8n gia' pronto via build_json_payload).
    Ritorna (telegram_ok, webhook_ok).
    """
    tg = _send_telegram(opp)
    wh = _send_webhook(opp)
    return tg, wh


# ---------------------------------------------------------------------------
# CLI / loop
# ---------------------------------------------------------------------------

def run_scan(notify: bool = True, log: bool = True) -> List[SurebetOpportunity]:
    """Esegue una scansione completa. Ritorna le opportunita' NUOVE segnalate."""
    opps = scan_all_sports()
    new = []
    for opp in opps:
        sig = _seen_signature(opp)
        if already_reported(sig):
            continue
        new.append(opp)
        if log:
            log_opportunity(opp)
        if notify:
            deliver_alert(opp)
        logger.info("surebet trovata: %s | %s %s @%.2f vs %s @%.2f | ROI +%.2f%%",
                    opp.evento, opp.esito_a, opp.bookmaker_a, opp.quota_a,
                    opp.bookmaker_b, opp.quota_b, opp.roi_pct)
    return new


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Surebet engine (The Odds API, h2h)")
    ap.add_argument("--loop", type=int, default=0,
                    help="Loop continuo ogni N secondi (0 = scan singolo)")
    ap.add_argument("--json", action="store_true",
                    help="Stampa JSON delle opportunita' (senza notifica)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scansiona ma NON notifica e NON logga")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    def _one_pass() -> None:
        if args.json:
            for opp in scan_all_sports():
                print(json.dumps(opp.to_dict(), ensure_ascii=False))
            return
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(json.dumps({
                "ts": time.time(), "sports": SPORTS, "pid": os.getpid(),
            }))
        except Exception as e:
            logger.warning("surebet heartbeat: %s", e)
        new = run_scan(notify=not args.dry_run, log=not args.dry_run)
        if args.dry_run:
            for opp in new:
                print(f"  {opp.evento} | {opp.esito_a}@{opp.quota_a:.2f} "
                      f"({opp.bookmaker_a}) vs {opp.esito_b}@{opp.quota_b:.2f} "
                      f"({opp.bookmaker_b}) | ROI +{opp.roi_pct:.2f}%")
            if not new:
                print("  nessuna opportunita' valida")

    logger.info("surebet cron: run avviato (sports=%s)", ",".join(SPORTS))
    _one_pass()
    # Hold opzionale a fine scan singolo (cron): tiene il container attivo
    # qualche secondo in piu' per diagnosi/ssh (env SUREBET_CRON_HOLD_SECONDS,
    # default 0 = disattivato). Innocuo col ciclo a 15' del cron.
    hold = int(os.getenv("SUREBET_CRON_HOLD_SECONDS", "0") or 0)
    if hold and not args.loop and not args.json:
        logger.info("surebet: hold %ds per diagnostica", hold)
        time.sleep(hold)
    logger.info("surebet cron: run completato")
    if args.loop:
        logger.info("surebet loop avviato: ogni %ds (CTRL+C per fermare)", args.loop)
        try:
            while True:
                time.sleep(args.loop)
                _one_pass()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())