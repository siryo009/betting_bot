"""tennis_quant.py — motore quantitativo TENNIS (ELO superficie + Poisson), 03/10/2026.

Direttiva del proprietario: potenziare il motore decisionale tennis con due
modelli che lavorino **in parallelo** al de-vigging di Shin e allo steam move,
non al posto loro. Questo modulo e' il pezzo che mancava.

⚠️ **MODO: SOLO MISURA (telemetria).** Su indicazione esplicita del proprietario
(03/10/2026) questo modulo **non piazza ordini e non tocca il percorso del
denaro**: calcola, registra nel ledger e logga. Lo stake reale degli ordini
resta quello del progetto (`auto_bet.order_stake`, importo fisso 1.50 USDC dal
28/09) e il Kelly calcolato qui e' **telemetria di confronto**, esposto ma non
applicato. Un tripwire lo verifica: nel sorgente non compare nessuna funzione
di ordine (`_live_fill`, `place_limit_order`, `resolve_market_for`,
`save_bet`), come per `significance.py`/`league_gate_impact.py`.

PERCHE' NON RICOPIARE NULLA (regola di progetto: "nessuna formula in due posti"):
- **ELO superficie-specifico** -> `SurfaceElo` EREDITA da
  `tennis_sandbox.TennisElo` (K, time-decay 30/60gg, blend superficie, seeding
  coerente). L'unica cosa riscritta e' la **persistenza**, che qui vive nel
  ledger SQLite principale come richiesto (`data/quotaverace.db`), mentre il
  sandbox paper continua col suo `ratings.json`.
- **De-vig di Shin** -> `market_calib.market_implied(..., method="shin")`
  (l'oracolo sharp `pinnacle_oracle` usa gia' Shin come default dal 02/10).
- **Kelly + cap** -> `value_filter.kelly_fraction` (frazione frazionaria del
  progetto) e il **recinto 40%** -> `auto_bet.exposure_allows` / `cap_order_stake`.
- **Superficie** -> `tennis_sandbox.detect_surface` (mai indovinare).
- **Discovery/memo SX** -> `tennis_lane.discover` (pubblica, zero crediti).

I DUE MODELLI E LA LORO INDIPENDENZA (il punto delicato):
1. modello intrinseco = combinazione di ELO e Poisson (hold/break);
2. modello di mercato = probabilita' fair di **Shin** sulle quote sharp.
Un giocatore senza storico viene **seminato** dal mercato sharp (miglior prior
disponibile), quindi finche' non ha partite saldate i due modelli **non sono
indipendenti**: il verdetto lo dichiara (`independent=False`, `model_mature`) e
distinguere i due casi e' esattamente cio' che serve per non leggere un
allineamento come una conferma. L'indipendenza arriva coi settlement
(`update_ratings_from_ledger`).

IL GATE "ENTRAMBI CONFERMANO": un lato e' candidato solo se
`EV_intrinseco >= soglia` **E** `EV_sharp >= soglia`, dove l'EV e' calcolato
sul **prezzo SX** (il prezzo che si pagherebbe). E' la lettura letterale della
direttiva: non basta che il modello veda valore, deve vederlo anche lo sharp.

CONSIGLIO SULL'INTEGRAZIONE ASINCRONA: `run_cycle()` e' CPU-only e senza rete
per default (la discovery ha memo 5'). Nel loop del bot va invocato in un thread
dell'executor, come `tennis_lane_job`:

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_scan_executor, tennis_quant.run_cycle)

Mai `await` diretto su `run_cycle()` se un domani vi si aggiungesse I/O: la
funzione e' sincrona e bloccherebbe il loop. Il job in `bot.py`
(`tennis_quant_job`, spento di default) usa esattamente questo pattern.

CLI:
    venv/bin/python tennis_quant.py --cycle           # un ciclo di misura
    venv/bin/python tennis_quant.py --update-ratings  # impara dai settlement
    venv/bin/python tennis_quant.py --report [--json] # istantanea
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ELO: classe base EREDITATA (nessuna formula ricopiata). L'import di
# `tennis_sandbox` porta con se' `requests`/`config`, non i moduli di denaro:
# `tracker`, `auto_bet`, `value_filter`, `market_calib` restano import PIGRI.
from tennis_sandbox import PlayerRating, SurfaceRating, TennisElo, detect_surface

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurazione (env-driven: una soglia e' una decisione, non un dettaglio)
# ---------------------------------------------------------------------------

#: Soglia EV dei due modelli. E' PROPRIA del tennis e dichiarata (il calcio usa
#: `value_filter.EV_MIN`, `tennis_lane` ha la sua): un unico posto la governa.
EV_MIN = float(os.getenv("TENNIS_QUANT_EV_MIN", "0.025"))

#: Peso dell'ELO nella combinazione col Poisson (0.5 = pari fiducia). Il Poisson
#: porta informazione di servizio/risposta, l'ELO la forza storica: entrambi
#: sono modelli "deboli" da soli, la combinazione riduce la varianza di ciascuno.
W_ELO = float(os.getenv("TENNIS_QUANT_W_ELO", "0.5"))

#: Poisson da hold/break. `RETURN_GAMES` = partite di risposta attese nel match
#: (best-of-3 ~ 10-12 ciascuno); `MAX_BREAKS` = troncamento della coda (λ < 5
#: per costruzione, la coda oltre 40 e' nulla).
RETURN_GAMES = float(os.getenv("TENNIS_QUANT_RETURN_GAMES", "11"))
MAX_BREAKS = int(os.getenv("TENNIS_QUANT_MAX_BREAKS", "40"))

#: Fallback hold/break quando NON c'e' uno stat provider: la probabilita' di
#: match viene tradotta in un differenziale di tenuta servizio attorno alla
#: media del circuito. E' un'APPROSSIMAZIONE DICHIARATA (l'ELO non conosce il
#: servizio): con uno stat provider iniettato si usa quello, mai il fallback.
BASE_HOLD = float(os.getenv("TENNIS_QUANT_BASE_HOLD", "0.80"))
HOLD_SPREAD = float(os.getenv("TENNIS_QUANT_HOLD_SPREAD", "0.10"))

#: Kelly di misura. La frazione e il cap NON toccano il denaro (telemetria): il
#: cap reale per singolo ordine resta quello di `auto_bet`.
KELLY_FRACTION = float(os.getenv("TENNIS_QUANT_KELLY_FRACTION", "0.25"))
MAX_STAKE_PCT = float(os.getenv("TENNIS_QUANT_MAX_STAKE_PCT", "0.02"))
MAX_STAKE_ABS = float(os.getenv("TENNIS_QUANT_MAX_STAKE_ABS", "25"))

#: Fascia quota della misura: la stessa del tennis live (1.30-2.50), letta da
#: `tennis_lane` quando disponibile per non avere due bande che divergono.
HOURS_AHEAD = float(os.getenv("TENNIS_QUANT_HOURS_AHEAD", "48"))

#: Numero minimo di partite saldate perche' il modello ELO sia "maturo" su un
#: giocatore (sotto, il verdetto e' dichiarato non indipendente).
MIN_MODEL_MATCHES = int(os.getenv("TENNIS_QUANT_MIN_MODEL_MATCHES", "3"))


def enabled() -> bool:
    """Interruttore (`TENNIS_QUANT_ENABLED=0` spegne tutto). Default ON.

    Il default e' ON perche' la modalita' e' SOLO-MISURA: un modulo che non
    tocca il denaro non deve restare spento per prudenza, deve produrre dati.
    """
    raw = os.getenv("TENNIS_QUANT_ENABLED")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Percorsi (ledger principale + log di telemetria)
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    """Ledger SQLite principale. Import PIGRO di `tracker` (pesante)."""
    raw = os.getenv("TENNIS_QUANT_DB")
    if raw:
        return Path(raw)
    try:
        import tracker
        return Path(tracker.DB_PATH)
    except Exception:                                          # pragma: no cover
        from config import DATA_DIR
        return Path(DATA_DIR) / "quotaverace.db"


def log_path() -> Path:
    """Path del log di telemetria. Env letta a OGNI chiamata, non all'import.

    ⚠️ Una costante a livello di modulo catturava l'env al PRIMO import: dopo,
    cambiarla (o isolarla nei test) non aveva effetto e le righe finivano nel
    file di PRODUZIONE. Il test di telemetria lo blinda.
    """
    raw = os.getenv("TENNIS_QUANT_LOG")
    if raw:
        return Path(raw)
    from config import DATA_DIR
    return Path(DATA_DIR) / "tennis_quant" / "evaluations.jsonl"


# ---------------------------------------------------------------------------
# Schema del ledger ELO nel DB principale (creazione idempotente, come
# `rating_engine.compute_ratings`: la tabella e' del modulo, non di tracker)
# ---------------------------------------------------------------------------

_ELO_SCHEMA = """
CREATE TABLE IF NOT EXISTS tennis_elo_ratings (
    player TEXT PRIMARY KEY,
    rating REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tennis_elo_surfaces (
    player TEXT NOT NULL,
    surface TEXT NOT NULL,
    rating REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (player, surface)
);
-- Idempotenza dell'apprendimento: un match saldato si applica UNA volta sola.
CREATE TABLE IF NOT EXISTS tennis_elo_applied (
    match_id TEXT NOT NULL,
    esito TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (match_id, esito)
);
"""


def _connect(db_path: Path) -> "Any":
    """Connessione al ledger col timeout (il bot scrive sullo stesso file)."""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:                                          # pragma: no cover
        pass
    conn.executescript(_ELO_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. ELO SUPERFICIE-SPECIFICO con persistenza nel ledger SQLite
# ---------------------------------------------------------------------------

class SurfaceElo(TennisElo):
    """ELO superficie-specifico di `tennis_sandbox.TennisElo`, su SQLite.

    Eredita TUTTA la matematica (prob, implied_rating, time-decay 30/60gg,
    blended rating superficie, seeding coerente di coppia, update con K pieno):
    qui si sostituiscono SOLO `load()` e `save()`, che leggono e scrivono le
    tabelle `tennis_elo_ratings` / `tennis_elo_surfaces` del ledger principale.
    Nessun'altra differenza, quindi il rating di questo modulo e quello del
    sandbox restano confrontabili (stessa formula, storage diverso).

    `db_path=None` -> ledger principale (`tracker.DB_PATH`).
    """

    def __init__(self, db_path: Optional[Path] = None,
                 k: Optional[float] = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        # Il file json del sandbox non viene mai toccato: `load()` e' override.
        super().__init__(ratings_file=self.db_path.parent / "_tennis_quant_unused.json",
                         k=k)

    # -- persistenza (le sole due funzioni riscritte) --------------------
    def load(self) -> None:
        """Carica i rating dalle tabelle del ledger. Fail-safe (DB assente -> 0)."""
        self.players = {}
        try:
            conn = _connect(self.db_path)
            try:
                for r in conn.execute(
                        "SELECT player, rating, n, last_ts FROM tennis_elo_ratings"):
                    self.players[str(r["player"])] = PlayerRating(
                        rating=float(r["rating"]), n=int(r["n"]),
                        last_ts=float(r["last_ts"]))
                for r in conn.execute(
                        "SELECT player, surface, rating, n, last_ts "
                        "FROM tennis_elo_surfaces"):
                    name = str(r["player"])
                    pr = self.players.get(name)
                    if pr is None:
                        continue
                    pr.surfaces[str(r["surface"])] = SurfaceRating(
                        rating=float(r["rating"]), n=int(r["n"]),
                        last_ts=float(r["last_ts"]))
            finally:
                conn.close()
        except Exception as e:
            logger.warning("tennis_quant: caricamento rating fallito (%s)", e)

    def save(self) -> None:
        """Scrive i rating nel ledger (upsert). Fail-safe: non solleva mai."""
        try:
            conn = _connect(self.db_path)
            try:
                ts = _now().isoformat()
                for name, pr in self.players.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO tennis_elo_ratings "
                        "(player, rating, n, last_ts, updated_at) VALUES (?,?,?,?,?)",
                        (name, float(pr.rating), int(pr.n), float(pr.last_ts), ts))
                    for surf, sr in (pr.surfaces or {}).items():
                        conn.execute(
                            "INSERT OR REPLACE INTO tennis_elo_surfaces "
                            "(player, surface, rating, n, last_ts) VALUES (?,?,?,?,?)",
                            (name, str(surf), float(sr.rating), int(sr.n),
                             float(sr.last_ts)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("tennis_quant: salvataggio rating fallito (%s)", e)

    # -- helper di lettura --------------------------------------------------
    def matches_played(self, player: str) -> int:
        """Partite saldate note per il giocatore (0 = solo seminato)."""
        pr = self.players.get((player or "").strip())
        return int(pr.n) if pr is not None else 0


# ---------------------------------------------------------------------------
# 2. MODELLO DI POISSON da hold/break (servizio/risposta)
# ---------------------------------------------------------------------------

def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) per X ~ Poisson(lam). Numericamente stabile via log-gamma."""
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    try:
        return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))
    except (ValueError, OverflowError):                        # pragma: no cover
        return 0.0


def poisson_match_prob(lam_a: float, lam_b: float,
                       max_events: Optional[int] = None) -> float:
    """P(A vince) da due processi di Poisson (break subiti/realizzati).

    Modello dichiarato: il numero di break che A e B realizzano sono variabili
    di Poisson indipendenti di media `lam_a` e `lam_b`; A vince se realizza
    piu' break. Un PAREGGIO di break non decide il match: vale 0.5 (neutralita'
    dichiarata, non un'ipotesi nascosta — nei fatti lo spareggio premia il
    servizio, che e' gia' dentro le medie).

        P(A) = P(Xa > Xb) + 0.5 * P(Xa == Xb)

    La coda e' troncata a `max_events` e la massa non troncata viene
    rinormalizzata, cosi' la probabilita' resta esatta anche con λ grandi.
    """
    events = int(max_events if max_events is not None else MAX_BREAKS)
    events = max(1, events)
    la = max(0.0, float(lam_a))
    lb = max(0.0, float(lam_b))
    pa = [poisson_pmf(k, la) for k in range(events + 1)]
    pb = [poisson_pmf(k, lb) for k in range(events + 1)]
    sa, sb = sum(pa), sum(pb)
    if sa > 0:
        pa = [p / sa for p in pa]
    if sb > 0:
        pb = [p / sb for p in pb]
    p_gt = 0.0
    p_eq = 0.0
    cum_b = 0.0
    for k in range(events + 1):
        p_gt += pa[k] * cum_b          # P(Xb < k)
        p_eq += pa[k] * pb[k]
        cum_b += pb[k]
    return max(0.0, min(1.0, p_gt + 0.5 * p_eq))


def break_rates(hold_a: float, hold_b: float,
                return_games: Optional[float] = None) -> Tuple[float, float]:
    """(λ_A, λ_B) = break attesi da A e da B sulle partite di risposta.

    `hold_x` = probabilita' che X vinca il proprio turno di servizio. A
    realizza un break sui turni di B con probabilita' (1 - hold_b), quindi
    λ_A dipende dalla tenuta di B (non dalla propria).

    ⚠️ L'attribuzione era invertita nella prima stesura (λ_A = 1 - hold_a):
    il favorito risultava sfavorito. Il test `test_favorito_di_servizio_
    favorito_anche_nel_match` la blinda.
    """
    games = float(return_games if return_games is not None else RETURN_GAMES)
    la = games * max(0.0, 1.0 - float(hold_b))      # break di A sui turni di B
    lb = games * max(0.0, 1.0 - float(hold_a))      # break di B sui turni di A
    return la, lb


def poisson_prob_from_hold_break(hold_a: float, hold_b: float, *,
                                 return_games: Optional[float] = None,
                                 max_events: Optional[int] = None) -> float:
    """P(A vince) dal modello di Poisson alimentato da hold/break."""
    la, lb = break_rates(hold_a, hold_b, return_games=return_games)
    return poisson_match_prob(la, lb, max_events=max_events)


def hold_rates_from_prob(p_a: float, *, base: Optional[float] = None,
                         spread: Optional[float] = None) -> Tuple[float, float]:
    """Hold/break APPROSSIMATI dalla probabilita' di match (nessuno stat reale).

    Fallback DICHIARATO per quando non c'e' un provider di statistiche di
    servizio: il differenziale di tenuta servizio scala linearmente con l'edge
    del match attorno alla media di circuito. Non e' una misura: e' un ponte
    per far girare il Poisson sulla sola informazione che abbiamo (l'ELO).
    Con uno stat provider iniettato questo percorso NON viene usato.
    """
    b = float(base if base is not None else BASE_HOLD)
    s = float(spread if spread is not None else HOLD_SPREAD)
    edge = max(-0.5, min(0.5, float(p_a) - 0.5))
    ha = min(0.95, max(0.50, b + edge * 2.0 * s))
    hb = min(0.95, max(0.50, b - edge * 2.0 * s))
    return ha, hb


# ---------------------------------------------------------------------------
# 3. MODELLO DI MERCATO (Shin) + combinazione + gate "entrambi confermano"
# ---------------------------------------------------------------------------

def shin_probabilities(odds_map: Dict[str, float],
                       *, method: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Probabilita' fair di SHIN su un mercato a 2 esiti.

    DELEGA a `market_calib.market_implied` (formula unica del progetto: qui non
    si ricopia il de-vig). `method` default = Shin; ritorna `{esito: p,
    "overround": ..., "method": ...}` oppure None (fail-closed) se mancano
    almeno due quote valide.
    """
    try:
        from market_calib import market_implied
    except Exception as exc:                                   # pragma: no cover
        logger.warning("tennis_quant: market_calib non disponibile (%s)", exc)
        return None
    meth = (method or os.getenv("TENNIS_QUANT_SHARP_METHOD", "shin")).strip().lower()
    if meth not in ("shin", "power", "multiplicative"):
        meth = "shin"
    clean = {k: float(v) for k, v in (odds_map or {}).items()
             if isinstance(v, (int, float)) and float(v) > 1.0}
    if len(clean) < 2:
        return None
    out = market_implied(clean, method=meth)
    if not out:
        return None
    out["method"] = meth
    return out


def combine_probabilities(p_elo: float, p_poisson: float,
                          w_elo: Optional[float] = None) -> float:
    """Combinazione lineare ELO + Poisson (peso dichiarato, mai nascosto).

    Entrambe le stime sono di un modello "debole": mescolarle riduce la
    varianza di ciascuna (stessa logica del blend modello/mercato di
    `market_calib.blend_probability`). Il peso `w_elo` e' la fiducia relativa
    nel rating storico; il Poisson porta l'informazione di servizio/risposta.
    """
    w = float(w_elo if w_elo is not None else W_ELO)
    w = max(0.0, min(1.0, w))
    p = w * float(p_elo) + (1.0 - w) * float(p_poisson)
    return max(1e-6, min(1.0 - 1e-6, p))


def expected_value(prob: float, odds: float) -> float:
    """EV per unita' di stake. Formula UNICA del progetto (`p*o - 1`)."""
    return float(prob) * float(odds) - 1.0


def evaluate(*, team_a: str, team_b: str, price_a: float, price_b: float,
             sharp_probs: Dict[str, float], elo: "SurfaceElo",
             surface: Optional[str] = None,
             serve_stats: Optional[Tuple[float, float]] = None,
             bankroll: float = 0.0, ev_min: Optional[float] = None,
             enclosure_check: Optional[Callable[[float, float], dict]] = None,
             now: Optional[float] = None) -> dict:
    """Verdetto quantitativo su UN mercato a 2 esiti. **Nessun ordine.**

    `sharp_probs` = probabilita' fair di Shin ({"1": p_a, "2": p_b}) prese dalle
    quote sharp; `price_a`/`price_b` = prezzo SX (quello che si pagherebbe).
    I due modelli sono indipendenti solo se `elo` ha storico reale: il verdetto
    lo dichiara.

    Ritorna un blocco con: p_elo, p_poisson, p_intrinsic, p_sharp, gli EV dei
    due modelli sul prezzo SX, `both_confirm` (entrambi >= soglia), il Kelly di
    misura e lo stato del recinto 40%. Fail-safe: su input incompleti ritorna
    `{"ok": False, "reason": ...}`, mai eccezioni.
    """
    try:
        e_min = float(ev_min if ev_min is not None else EV_MIN)
        p_sharp_a = float(sharp_probs.get("1"))
        p_sharp_b = float(sharp_probs.get("2"))
    except (TypeError, ValueError, AttributeError):
        return {"ok": False, "reason": "sharp_probs_invalidi"}
    if not (0.0 < p_sharp_a < 1.0) or not (0.0 < p_sharp_b < 1.0):
        return {"ok": False, "reason": "sharp_probs_fuori_range"}
    pa, pb = float(price_a), float(price_b)
    if pa <= 1.0 or pb <= 1.0:
        return {"ok": False, "reason": "prezzo_non_valido"}

    ts = float(now if now is not None else time.time())
    n_a = elo.matches_played(team_a)
    n_b = elo.matches_played(team_b)

    # --- modello 1: ELO superficie-specifico -----------------------------
    p_elo = elo.match_prob(team_a, team_b, surface=surface)

    # --- modello 1b: Poisson da hold/break -------------------------------
    if serve_stats is not None:
        hold_a, hold_b = float(serve_stats[0]), float(serve_stats[1])
        stats_source = "provider"
    else:
        hold_a, hold_b = hold_rates_from_prob(p_elo)
        stats_source = "elo_fallback"
    p_poisson = poisson_prob_from_hold_break(hold_a, hold_b)

    p_intrinsic = combine_probabilities(p_elo, p_poisson)

    # --- modello 2: mercato (Shin) ---------------------------------------
    p_sharp = p_sharp_a

    ev_intrinsic_a = expected_value(p_intrinsic, pa)
    ev_sharp_a = expected_value(p_sharp, pa)
    ev_intrinsic_b = expected_value(1.0 - p_intrinsic, pb)
    ev_sharp_b = expected_value(1.0 - p_sharp, pb)

    model_mature = n_a >= MIN_MODEL_MATCHES and n_b >= MIN_MODEL_MATCHES
    # Se almeno un giocatore e' solo seminato, il modello intrinseco "contiene"
    # il mercato: i due EV non sono due prove indipendenti.
    independent = bool(model_mature and stats_source != "elo_fallback")

    # --- gate "entrambi confermano" (lato migliore, sul prezzo SX) --------
    sides = {
        "1": {"price": pa, "p_intrinsic": p_intrinsic, "p_sharp": p_sharp,
              "ev_intrinsic": ev_intrinsic_a, "ev_sharp": ev_sharp_a},
        "2": {"price": pb, "p_intrinsic": 1.0 - p_intrinsic,
              "p_sharp": p_sharp_b, "ev_intrinsic": ev_intrinsic_b,
              "ev_sharp": ev_sharp_b},
    }
    best = max(sides.items(), key=lambda kv: kv[1]["ev_intrinsic"])
    side_key, side = best
    both_confirm = bool(side["ev_intrinsic"] >= e_min
                        and side["ev_sharp"] >= e_min)

    # --- fascia quota (difesa in profondita', stessa del tennis live) -----
    try:
        import tennis_lane as tl
        band_ok = bool(tl.in_odds_band(side["price"]))
        odds_min, odds_max = tl.ODDS_MIN, tl.ODDS_MAX
    except Exception:                                          # pragma: no cover
        band_ok = True
        odds_min, odds_max = None, None

    # --- Kelly di MISURA (nessun denaro: il tetto resta `order_stake`) ----
    kelly = _kelly_telemetry(p_intrinsic=side["p_intrinsic"], price=side["price"],
                             bankroll=bankroll, side_key=side_key,
                             enclosure_check=enclosure_check)

    return {
        "ok": True,
        "team_a": team_a, "team_b": team_b,
        "surface": surface,
        "price_a": round(pa, 4), "price_b": round(pb, 4),
        "p_elo": round(p_elo, 6),
        "p_poisson": round(p_poisson, 6),
        "p_intrinsic": round(p_intrinsic, 6),
        "p_sharp_a": round(p_sharp_a, 6),
        "p_sharp_b": round(p_sharp_b, 6),
        "hold_a": round(hold_a, 4), "hold_b": round(hold_b, 4),
        "serve_stats_source": stats_source,
        "elo_matches_a": n_a, "elo_matches_b": n_b,
        "model_mature": bool(model_mature),
        "independent": independent,
        "side": side_key,
        "ev_intrinsic": round(side["ev_intrinsic"], 6),
        "ev_sharp": round(side["ev_sharp"], 6),
        "ev_min": e_min,
        "both_confirm": both_confirm,
        "candidate": bool(both_confirm and band_ok),
        "band_ok": band_ok, "odds_min": odds_min, "odds_max": odds_max,
        "kelly": kelly,
        "evaluated_at": ts,
    }


def _kelly_telemetry(*, p_intrinsic: float, price: float, bankroll: float,
                     side_key: str,
                     enclosure_check: Optional[Callable[[float, float], dict]]
                     ) -> dict:
    """Kelly di MISURA + recinto 40% (delega, nessuna soglia ricopiata).

    Ritorna la size suggerita dal Kelly frazionario, il cap percentuale e lo
    stato del recinto. ⚠️ Non e' lo stake dell'ordine: quello resta
    `auto_bet.order_stake` (1.50 USDC fissi). Serve a misurare quanto sarebbe
    diversa la size se il Kelly governasse, senza cambiare il denaro.
    """
    out: Dict[str, Any] = {"fraction": None, "stake_raw": None, "cap": None,
                           "suggested_stake": None, "enclosure": None}
    try:
        from value_filter import kelly_fraction
        frac = kelly_fraction(float(p_intrinsic), float(price), KELLY_FRACTION)
        out["fraction"] = round(frac, 6)
        raw = max(0.0, float(bankroll) * frac)
        cap = min(float(bankroll) * MAX_STAKE_PCT, MAX_STAKE_ABS)
        out["stake_raw"] = round(raw, 4)
        out["cap"] = round(cap, 4)
        out["suggested_stake"] = round(min(raw, cap), 2)
    except Exception as exc:                                   # pragma: no cover
        logger.debug("tennis_quant: kelly non calcolabile (%s)", exc)
    # Recinto 40%: una sola definizione (auto_bet) — qui solo lettura.
    try:
        if enclosure_check is not None:
            out["enclosure"] = enclosure_check(float(bankroll),
                                               float(out["suggested_stake"] or 0.0))
        else:
            import auto_bet
            out["enclosure"] = auto_bet.exposure_allows(
                float(bankroll), float(out["suggested_stake"] or 0.0))
    except Exception as exc:                                   # pragma: no cover
        out["enclosure"] = {"allowed": None, "reason": f"non leggibile ({exc})"}
    return out


# ---------------------------------------------------------------------------
# 4. Riferimento sharp (Shin) — dalla cache, zero crediti
# ---------------------------------------------------------------------------

def sharp_reference(team_a: str, team_b: str, *,
                    lookup: Optional[Callable[[str, str], Optional[dict]]] = None
                    ) -> Optional[Dict[str, Any]]:
    """Probabilita' fair di Shin per la coppia, dalle cache sharp del progetto.

    DELEGA a `pinnacle_oracle.load_oracle(..., outcomes=("1","2"))`: quel
    percorso usa gia' Shin come default dal 02/10 e legge le cache che la
    rotazione quote scarica (0 crediti, nessuna rete). Fail-closed: None se la
    partita non e' coperta o se lo sharp non ha tutti e due gli esiti.
    """
    if lookup is not None:
        return lookup(team_a, team_b)
    try:
        import pinnacle_oracle as po
        raw = po.load_oracle(team_a, team_b, outcomes=("1", "2"))
    except Exception as exc:
        logger.debug("tennis_quant: oracolo non disponibile (%s)", exc)
        return None
    if not raw:
        return None
    try:
        p1, p2 = float(raw.get("1")), float(raw.get("2"))
    except (TypeError, ValueError):
        return None
    if not (0.0 < p1 < 1.0 and 0.0 < p2 < 1.0):
        return None
    return {
        "1": p1, "2": p2,
        "method": raw.get("devig_method") or os.getenv(
            "TENNIS_QUANT_SHARP_METHOD", "shin"),
        "shin_z": raw.get("shin_z"),
        "sources": raw.get("sources"),
        "overround": raw.get("overround"),
    }


# ---------------------------------------------------------------------------
# 5. CICLO DI MISURA (telemetria: ledger ELO + JSONL, MAI ordini)
# ---------------------------------------------------------------------------

def _write_telemetry(row: dict, *, path: Optional[Path] = None) -> bool:
    """Appende una riga JSONL. Fail-safe: non solleva mai."""
    target = Path(path) if path else log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception as exc:
        logger.warning("tennis_quant: telemetria non scritta (%s)", exc)
        return False


def run_cycle(*, provider: Any = None, sharp_lookup: Optional[Callable] = None,
              serve_stats_lookup: Optional[Callable] = None,
              bankroll: Optional[float] = None,
              enclosure_check: Optional[Callable[[float, float], dict]] = None,
              write: bool = True, persist_ratings: bool = True) -> dict:
    """UN ciclo di misura: discovery -> ELO+Poisson+Shin -> telemetria.

    **Non piazza ordini e non scrive nel ledger delle puntate.** Scrive solo
    (a) il log JSONL di telemetria e (b) i rating ELO aggiornati (seed dal
    mercato per i giocatori nuovi). Pensata per girare in un thread dell'
    executor, mai in `await` diretto sul loop.

    Fail-safe totale: qualunque errore -> `{"error": ...}`, mai eccezioni.
    """
    out: Dict[str, Any] = {
        "enabled": enabled(), "events": 0, "evaluated": 0,
        "candidates": 0, "both_confirm": 0, "independent": 0,
        "skipped_no_sharp": 0, "errors": 0, "error": None, "rows": [],
    }
    if not enabled():
        return out
    try:
        import tennis_lane as tl
    except Exception as exc:
        out["error"] = f"tennis_lane non disponibile ({exc})"
        return out
    if bankroll is None:
        try:
            import auto_bet
            snap = auto_bet._live_wallet_snapshot()
            bankroll = float(snap["equity"]) if snap else 0.0
        except Exception:
            bankroll = 0.0
    elo = SurfaceElo()
    try:
        events = tl.discover(provider=provider)
    except Exception as exc:
        out["error"] = f"discovery fallita ({exc})"
        return out
    out["events"] = len(events)
    touched = False
    for ev in events:
        try:
            t1, t2 = ev["team_one"], ev["team_two"]
            prices = {s["key"]: float(s["price"]) for s in ev["sides"]}
            sharp = sharp_reference(t1, t2, lookup=sharp_lookup)
            if not sharp:
                out["skipped_no_sharp"] += 1
                continue
            # Seed coerente dei giocatori NUOVI dallo sharp (miglior prior).
            # Non sovrascrive chi ha gia' storico: la maturita' cresce coi
            # settlement, non col seeding.
            # ⚠️ Il rilevamento del cambiamento guarda l'INSIEME dei giocatori,
            # non `matches_played`: un giocatore seminato ha n=0, quindi il
            # confronto sui conteggi non vedeva il seeding e i rating non
            # venivano mai salvati (bug colto dal test di persistenza).
            before = set(elo.players)
            elo.ensure_pair(t1, float(sharp["1"]), t2, float(sharp["2"]))
            if set(elo.players) != before:
                touched = True
            serve_stats = None
            if serve_stats_lookup is not None:
                serve_stats = serve_stats_lookup(t1, t2)
            surface = detect_surface(ev.get("league_label"))
            verdict = evaluate(
                team_a=t1, team_b=t2,
                price_a=prices.get("1", 0.0), price_b=prices.get("2", 0.0),
                sharp_probs=sharp, elo=elo, surface=surface,
                serve_stats=serve_stats, bankroll=float(bankroll or 0.0),
                enclosure_check=enclosure_check)
            if not verdict.get("ok"):
                out["errors"] += 1
                continue
            out["evaluated"] += 1
            if verdict.get("both_confirm"):
                out["both_confirm"] += 1
            if verdict.get("candidate"):
                out["candidates"] += 1
            if verdict.get("independent"):
                out["independent"] += 1
            row = {
                "ts": _now().isoformat(),
                "market_id": ev.get("market_id"),
                "league": ev.get("league_label"),
                "sharp_sources": sharp.get("sources"),
                "sharp_method": sharp.get("method"),
                **verdict,
            }
            out["rows"].append(row)
            if write:
                _write_telemetry(row)
        except Exception as exc:
            out["errors"] += 1
            logger.warning("tennis_quant: evento non valutato (%s)", exc)
    if persist_ratings and touched:
        elo.save()
    return out


def update_ratings_from_ledger(*, db_path: Optional[Path] = None,
                               elo: Optional["SurfaceElo"] = None,
                               limit: int = 500) -> dict:
    """Impara dai settlement: aggiorna l'ELO coi match tennis SALDATI.

    Legge `predictions` (mercato TENNIS, `esito_finale` noto) JOIN `matches`
    per i nomi e la lega (da cui la superficie). Idempotente grazie a
    `tennis_elo_applied` (un match saldato si applica UNA volta sola): senza
    quel marker un secondo giro double-applicherebbe l'aggiornamento e i
    rating divergerebbero dal sandbox.

    Fail-safe: qualunque errore -> dict con `error`, mai eccezioni.
    """
    out = {"applied": 0, "skipped": 0, "error": None}
    path = Path(db_path) if db_path else default_db_path()
    elo = elo or SurfaceElo(db_path=path)
    try:
        conn = _connect(path)
    except Exception as exc:
        out["error"] = f"ledger non leggibile ({exc})"
        return out
    try:
        rows = conn.execute(
            """SELECT p.match_id, p.esito, p.esito_finale,
                      m.home_team, m.away_team, m.league
                 FROM predictions p
                 LEFT JOIN matches m ON m.id = p.match_id
                WHERE p.mercato = 'TENNIS'
                  AND p.esito_finale IS NOT NULL
                ORDER BY p.settled_at DESC
                LIMIT ?""", (int(limit),)).fetchall()
        for r in rows:
            home, away = r["home_team"], r["away_team"]
            if not home or not away:
                out["skipped"] += 1
                continue
            done = conn.execute(
                "SELECT 1 FROM tennis_elo_applied WHERE match_id=? AND esito=?",
                (r["match_id"], r["esito"])).fetchone()
            if done:
                out["skipped"] += 1
                continue
            esito, fin = str(r["esito"]), str(r["esito_finale"]).lower()
            if esito not in ("1", "2") or fin not in ("won", "lost", "push"):
                out["skipped"] += 1
                continue
            if fin == "push":
                # Nessun vincitore: non si apprende nulla (void/ritiro).
                conn.execute(
                    "INSERT OR REPLACE INTO tennis_elo_applied "
                    "(match_id, esito, applied_at) VALUES (?,?,?)",
                    (r["match_id"], r["esito"], _now().isoformat()))
                out["skipped"] += 1
                continue
            side_won = (esito == "1") == (fin == "won")
            winner = home if side_won else away
            loser = away if side_won else home
            try:
                elo.update(winner, loser,
                           surface=detect_surface(r["league"]))
            except Exception as exc:
                logger.warning("tennis_quant: update ELO fallito (%s)", exc)
                out["skipped"] += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO tennis_elo_applied "
                "(match_id, esito, applied_at) VALUES (?,?,?)",
                (r["match_id"], r["esito"], _now().isoformat()))
            out["applied"] += 1
        conn.commit()
        if out["applied"]:
            elo.save()
    except Exception as exc:
        out["error"] = str(exc)
        logger.warning("tennis_quant: update dal ledger (%s)", exc)
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# 6. REPORT
# ---------------------------------------------------------------------------

def _read_telemetry(limit: int = 200) -> List[dict]:
    """Ultime righe di telemetria (piu' recenti in coda). Fail-safe."""
    path = log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            continue
    return out


def summary(limit: int = 200) -> dict:
    """Istantanea della misura: righe di telemetria + stato dei rating."""
    rows = _read_telemetry(limit=limit)
    n = len(rows)
    cand = sum(1 for r in rows if r.get("candidate"))
    both = sum(1 for r in rows if r.get("both_confirm"))
    indep = sum(1 for r in rows if r.get("independent"))
    avg = (sum(float(r.get("ev_intrinsic") or 0.0) for r in rows) / n) if n else None
    out = {
        "enabled": enabled(), "log": str(log_path()),
        "rows": n, "candidates": cand, "both_confirm": both,
        "independent": indep,
        "avg_ev_intrinsic": round(avg, 6) if avg is not None else None,
        "ev_min": EV_MIN, "w_elo": W_ELO,
        "elo_players": 0, "elo_surfaces": 0,
    }
    try:
        conn = _connect(default_db_path())
        try:
            out["elo_players"] = int(conn.execute(
                "SELECT COUNT(*) FROM tennis_elo_ratings").fetchone()[0])
            out["elo_surfaces"] = int(conn.execute(
                "SELECT COUNT(*) FROM tennis_elo_surfaces").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        out["elo_error"] = str(exc)
    return out


def format_report(block: Optional[dict] = None) -> str:
    s = block if isinstance(block, dict) else summary()
    lines = ["🎾 Tennis Quant (ELO superficie + Poisson + Shin) — SOLO MISURA"]
    if not s.get("enabled"):
        lines.append("  • SPENTO (TENNIS_QUANT_ENABLED=0)")
        return "\n".join(lines)
    lines.append(f"  • soglia EV {float(s.get('ev_min') or 0) * 100:.1f}% | "
                 f"peso ELO {float(s.get('w_elo') or 0):.2f}")
    lines.append(f"  • valutazioni registrate: {s.get('rows')} | "
                 f"candidati (entrambi i modelli): {s.get('candidates')} | "
                 f"conferme: {s.get('both_confirm')}")
    lines.append(f"  • verdetti su modello maturo: {s.get('independent')}")
    if s.get("avg_ev_intrinsic") is not None:
        lines.append(f"  • EV intrinseco medio: "
                     f"{float(s['avg_ev_intrinsic']) * 100:+.2f}%")
    lines.append(f"  • rating ELO nel ledger: {s.get('elo_players')} giocatori "
                 f"({s.get('elo_surfaces')} righe superficie)")
    lines.append("  ℹ️ nessun ordine: il modulo misura, lo stake reale resta "
                 "quello del progetto (1.50 USDC fissi)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Motore quantitativo tennis (misura, nessun ordine)")
    ap.add_argument("--cycle", action="store_true",
                    help="un ciclo di misura (discovery + ELO/Poisson/Shin)")
    ap.add_argument("--update-ratings", action="store_true",
                    help="aggiorna l'ELO dai match tennis saldati")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.update_ratings:
        res = update_ratings_from_ledger()
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
              else f"rating: applicati {res['applied']} | saltati "
                   f"{res['skipped']} | errore {res['error']}")
    if args.cycle:
        res = run_cycle()
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
              else f"ciclo: eventi {res['events']} | valutati {res['evaluated']} "
                   f"| candidati {res['candidates']} | senza sharp "
                   f"{res['skipped_no_sharp']}")
    if args.report or not (args.cycle or args.update_ratings):
        s = summary()
        print(json.dumps(s, indent=2, ensure_ascii=False) if args.json
              else format_report(s))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
