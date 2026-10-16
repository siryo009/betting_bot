"""Feature engineering GRATUITO per il modello ML (modulo 1, `QUANT_MODULES.md`).

Feature di STORIA + stato cronologico
-------------------------------------
Oltre alle feature "di forma" (riposo, densita' di calendario, forma recente,
xG) il modulo costruisce lo **stato cronologico** di ogni partita: l'**Elo**
delle due squadre PRIMA del kickoff e il **contesto di lega** (media gol della
lega e quante partite di quella lega sono gia' state viste). Sono le feature
che mancavano per due ragioni opposte: l'Elo riassume TUTTA la storia di una
squadra in un numero (le medie a finestra vedono solo le ultime 5 partite) e il
contesto di lega dice al modello in che tipo di campionato sta giocando —
informazione che Poisson ricava a mano dalle tabelle di lega.
Perche' un Elo in casa e non un servizio esterno: `live_intel` puo' leggere
ClubElo, ma e' un provider ESTERNO (spento offline, non sempre raggiungibile) e
il progetto non ha un Elo di calcio proprio.

Perche' esiste
--------------
`ml_ensemble.py` addestra un booster (`xgboost` se presente, altrimenti la
Logistic Regression numpy-only) su 15 feature che descrivono **il modello**
(`lam_h`, `lam_a`, `prob_1/X/2/over`) e **il mercato** (quota, `market_prob`,
`market_edge`, `ev`). Sono tutte feature "istantanee": nessuna sa *cosa e'
successo prima*, quindi il booster puo' solo rimappare cio' che Poisson ha gia'
detto. Questo modulo produce le feature che mancavano — **riposo, densita' di
calendario, forma recente, xG** — da dati che abbiamo GIA' e a costo zero
(`match_results`, 17k righe storiche; `live_intel` per l'xG).

L'invariante centrale: NESSUN LOOK-AHEAD
----------------------------------------
Le feature di una partita al kickoff `T` usano SOLO risultati con data `< T`
(strettamente prima: la partita stessa e' esclusa). Un modello addestrato con
feature che vedono il futuro misura se stesso, non il gioco — ed e' l'errore
piu' facile da commettere in un feature builder. Un test dedicato aggiunge un
risultato SUCCESSIVO e pretende che le feature della partita precedente non
cambino.

Due popolazioni, e non si confondono
------------------------------------
- **storica** (`history_feature_rows`): 17k partite, label = esito reale. E' la
  popolazione su cui si SCREENANO le feature (campione grande = la scrematura
  dice qualcosa).
- **live** (`features_for`): i match del ledger corrente, dove l'xG di
  stagione ha senso perche' descrive la squadra NELL'ISTANTE della decisione.
⚠️ Per questo l'xG e' **escluso** dalla scrematura storica
(`LOOKAHEAD_UNSAFE`): un aggregato di stagione include le partite SUCCESSIVE al
kickoff. Sulla storia sarebbe look-ahead, quindi non entra — dichiarato, non
dimenticato.

Cosa NON fa questo modulo
-------------------------
- **non tocca le probabilita' di produzione**: `ml_ensemble.FEATURE_NAMES` non
  viene modificato. Un cambio di feature cambia le probabilita' che il bot usa,
  e la porta di uscita (Brier walk-forward migliore) non e' ancora misurata.
- **non decide niente**: nessun gate, nessuno stake, nessun ordine. E' materia
  prima + la misura che serve per PESCARE le feature prima di addestrare.
- **non stima i valori mancanti**: una feature assente resta `None`. Uno 0
  inventato e uno 0 vero insegnano due cose diverse a un modello — esattamente
  la regola dell'xG in `live_intel`.

Garanzie (tripwire in `test_match_features.py`)
----------------------------------------------
sola LETTURA (`mode=ro`, il test tenta una `UPDATE` e pretende che il DB la
rifiuti), zero rete (l'xG si attiva solo con `--xg`/`xg_fn`), zero crediti,
zero ordini, nessuna scrittura sul ledger, `import match_features` che NON
carica `tracker`/`auto_bet`/`bot` (verificato in sottoprocesso).

CLI
---
  venv/bin/python match_features.py --history --screening
  venv/bin/python match_features.py --match "Inter" "Napoli" --kickoff 2026-10-18T18:00:00Z
  venv/bin/python match_features.py --history --coverage [--json] [--db PATH]
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabolario delle feature (una sola definizione: nomi + ordine)
# ---------------------------------------------------------------------------

#: Finestre di densita' di calendario misurate (giorni prima del kickoff).
REST_WINDOWS: Tuple[int, ...] = (7, 14, 28)
#: Soglia di "calendario congestionato": partite negli ultimi 7 giorni.
CONGESTED_MATCHES_7D = 3
#: Soglia di "riposo corto" (giorni fra due partite).
SHORT_REST_DAYS = 3
#: Partite su cui si misura la forma recente (media punti/gol).
FORM_N = 5

FEATURE_NAMES: Tuple[str, ...] = (
    "rest_days_home", "rest_days_away",
    "matches_7d_home", "matches_7d_away",
    "matches_14d_home", "matches_14d_away",
    "matches_28d_home", "matches_28d_away",
    "congested_home", "congested_away",
    "short_rest_home", "short_rest_away",
    "hist_matches_home", "hist_matches_away",
    "pts5_home", "pts5_away",
    "gf5_home", "gf5_away",
    "ga5_home", "ga5_away",
    "xg_for_home", "xg_against_home",
    "xg_for_away", "xg_against_away",
    "xg_present",
    "elo_home", "elo_away", "elo_diff", "elo_expect_home",
    "league_goal_avg", "league_prev_matches",
    "is_weekend", "kickoff_hour", "month",
)

# --- Elo di calcio, definizione UNICA del progetto -------------------------
# Perche' qui e non in un modulo condiviso: il progetto ha un ELO **tennis**
# (`tennis_sandbox`, superficie-specifico, con seeding dal mercato) e nessun Elo
# di CALCIO in casa; l'unica alternativa in progetto e' ClubElo via `live_intel`
# (provider ESTERNO, non interrogabile offline e non sempre raggiungibile).
# Questi tre numeri sono la formula classica (World Football Elo Ratings):
#   atteso_casa = 1 / (1 + 10^(-(elo_casa + VANTAGGIO_CASA - elo_ospite)/400))
#   nuovo       = elo + K x indice_margine x (punteggio - atteso)
# con punteggio 1/0.5/0 e indice del margine di gol 1.0 / 1.5 / (11+gd)/8.
# E' materia PRIMA: non tocca nessuna probabilita' di produzione.
ELO_BASE = 1500.0               # rating di una squadra mai vista (prior neutro)
ELO_K = 20.0                    # velocita' di aggiornamento
ELO_HOME_ADVANTAGE = 65.0       # vantaggio campo in punti Elo (convenzione ~1/3 di gol)


def goal_margin_index(goal_diff: int) -> float:
    """Indice del margine (World Football Elo): 1.0, 1.5, poi (11+gd)/8."""
    gd = abs(int(goal_diff))
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


def elo_expected(elo_home: float, elo_away: float, *,
                 home_advantage: float = ELO_HOME_ADVANTAGE) -> float:
    """Punteggio atteso della squadra di casa (0..1) secondo l'Elo."""
    try:
        delta = float(elo_home) + float(home_advantage) - float(elo_away)
    except (TypeError, ValueError):
        return 0.5
    return 1.0 / (1.0 + 10.0 ** (-delta / 400.0))


def elo_update(elo_home: float, elo_away: float, score_home: int, score_away: int,
               *, k: float = ELO_K,
               home_advantage: float = ELO_HOME_ADVANTAGE) -> Tuple[float, float]:
    """Nuovi rating dopo il risultato (vittoria casa = 1, pari = 0.5)."""
    if score_home > score_away:
        score = 1.0
    elif score_home == score_away:
        score = 0.5
    else:
        score = 0.0
    expected = elo_expected(elo_home, elo_away, home_advantage=home_advantage)
    delta = float(k) * goal_margin_index(score_home - score_away) * (score - expected)
    return float(elo_home) + delta, float(elo_away) - delta

#: Feature il cui valore, calcolato sulla STORIA, conterrebbe informazione
#: futura: l'xG di `live_intel` e' un aggregato di STAGIONE, quindi include le
#: partite successive al kickoff. Restano disponibili per i match CORRENTI
#: (dove sono la fotografia della squadra oggi) e sono **escluse** dalla
#: scrematura storica.
LOOKAHEAD_UNSAFE: Tuple[str, ...] = (
    "xg_for_home", "xg_against_home",
    "xg_for_away", "xg_against_away",
    "xg_present",
)

#: Sottoinsieme usabile senza look-ahead (e' quello che la scrematura misura).
HISTORY_FEATURES: Tuple[str, ...] = tuple(
    n for n in FEATURE_NAMES if n not in LOOKAHEAD_UNSAFE)

#: Campione minimo perche' un AUC sia leggibile (stessa disciplina di
#: `portfolio_risk.MIN_PAIRS` e `significance.MIN_SAMPLES`: sotto, rumore).
MIN_LABELS = 200
#: Effetto minimo perche' una feature valga la pena di essere addestrata
#: (0.02 di AUC = 2 punti di probabilabilita' di ranking corretto).
AUC_EPS = 0.02
#: Soglia del test di Mann-Whitney (bilaterale).
P_MAX = 0.05

#: Target disponibili per la scrematura (label binaria dai risultati reali).
TARGETS = ("home_win", "home_or_draw", "away_win", "over25")


# ---------------------------------------------------------------------------
# Lettura del ledger (sola lettura, path pigro)
# ---------------------------------------------------------------------------

def db_path() -> Path:
    """Percorso del ledger: da `tracker.DB_PATH` (import pigro)."""
    from tracker import DB_PATH  # import pigro: `import match_features` resta leggero
    return Path(DB_PATH)


def ro_conn(path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connessione **READ-ONLY**: questa diagnostica non scrive mai."""
    target = Path(path) if path else db_path()
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_ts(raw: Any, *, assume_utc: bool = False) -> Optional[datetime]:
    """ISO -> datetime UTC. `None` se illeggibile o SENZA fuso orario.

    Un istante senza fuso e' ambiguo e il progetto non lo indovina (regola del
    15/09): il chiamante che SA che la fonte e' UTC passa `assume_utc=True`, e
    i lettori contano quanti valori hanno assunto (campo `naive_ts` nel
    report).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        if not assume_utc:
            return None
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _epoch(raw: Any, *, assume_utc: bool = True) -> Optional[float]:
    """Istante -> epoch (secondi). Accetta ISO **e** un epoch gia' numerico.

    L'epoch passa attraverso la stessa funzione dell'ISO: senza, un percorso
    interno che ha gia' convertito (lo storico) e uno che riceve una data dal
    ledger non si parlerebbero (bug reale: 17k righe scartate come "squadra
    sconosciuta"). Un `bool` NON e' un epoch.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    d = parse_ts(raw, assume_utc=assume_utc)
    return d.timestamp() if d else None


def _norm_key(name: Any) -> str:
    """Chiave di indice interna (deterministica, in casa).

    Non e' un resolver di nomi: serve solo a indicizzare. Il riconoscimento
    tollerante (sigle, prefissi di club) e' delegato a `team_names.same_team`,
    che e' l'unica definizione del progetto.
    """
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


# ---------------------------------------------------------------------------
# Indice della storia (bisect per squadra, senza look-ahead per costruzione)
# ---------------------------------------------------------------------------

#: Una voce di storia: (ts_epoch, match_id, gol_fatti, gol_subiti, is_home).
Entry = Tuple[float, str, int, int, bool]


class TeamIndex:
    """Storico per squadra, ordinato nel tempo, con riconoscimento dei nomi.

    Il riconoscimento esatto e' per chiave normalizzata; se la chiave non
    esiste si prova `team_names.same_team` sull'elenco dei nomi noti (una
    scansione, MEMOIZZATA per nome richiesto: la prima ricerca paga, le
    successive no). Mai fuzzy: `same_team` e' deterministico.
    """

    def __init__(self, history: Sequence[Dict[str, Any]], *,
                 allow_alias: bool = True) -> None:
        self._by_key: Dict[str, List[Entry]] = {}
        self._names: Dict[str, str] = {}          # chiave -> nome rappresentativo
        self._memo: Dict[str, Optional[str]] = {}
        self._allow_alias = allow_alias
        for row in history:
            ts = float(row["ts"])
            sh, sa = int(row["score_home"]), int(row["score_away"])
            mid = str(row.get("match_id") or "")
            for team, gf, ga, is_home in ((row["home_team"], sh, sa, True),
                                          (row["away_team"], sa, sh, False)):
                key = _norm_key(team)
                if not key:
                    continue
                self._by_key.setdefault(key, []).append((ts, mid, gf, ga, is_home))
                self._names.setdefault(key, str(team))
        for key in self._by_key:
            self._by_key[key].sort(key=lambda e: e[0])

    def resolve(self, team: Any) -> Optional[str]:
        """Chiave dell'indice per un nome di squadra (`None` se sconosciuta)."""
        key = _norm_key(team)
        if not key:
            return None
        if key in self._by_key:
            return key
        if key in self._memo:
            return self._memo[key]
        resolved: Optional[str] = None
        if self._allow_alias:
            try:
                from team_names import same_team  # import pigro
            except Exception:
                same_team = None
            if same_team is not None:
                name = str(team or "")
                hits = [k for k, rep in self._names.items()
                        if same_team(name, rep)]
                # Guardia di ambiguita': due candidati "simili" = non si
                # indovina (stessa regola di `team_names`).
                if len(hits) == 1:
                    resolved = hits[0]
        self._memo[key] = resolved
        return resolved

    def known_keys(self) -> List[str]:
        return list(self._by_key)

    def matches_before(self, team: Any, kickoff_epoch: float) -> List[Entry]:
        """Partite della squadra con `ts < kickoff` (STRETTAMENTE prima)."""
        key = self.resolve(team)
        if key is None:
            return []
        entries = self._by_key[key]
        idx = bisect.bisect_left([e[0] for e in entries], kickoff_epoch)
        return entries[:idx]

    def all_matches(self, team: Any) -> List[Entry]:
        key = self.resolve(team)
        return list(self._by_key.get(key or "", []))


def build_index(conn: sqlite3.Connection, *, allow_alias: bool = True,
                limit: Optional[int] = None
                ) -> Tuple[TeamIndex, Dict[str, int]]:
    """Costruisce l'indice dallo storico dei risultati (sola lettura)."""
    stats = {"rows": 0, "rows_used": 0, "bad_ts": 0, "bad_score": 0,
             "naive_ts": 0, "bad_names": 0}
    try:
        rows = conn.execute(
            "SELECT match_id, league, home_team, away_team, score_home, "
            "score_away, result, settled_at FROM match_results"
        ).fetchall()
    except sqlite3.Error as exc:                     # tabella assente/DB vecchio
        logger.warning("match_features: storia illeggibile (%s)", exc)
        return TeamIndex([]), stats
    history: List[Dict[str, Any]] = []
    for r in rows:
        stats["rows"] += 1
        raw_ts = r["settled_at"]
        ts = _epoch(raw_ts, assume_utc=True)
        if ts is None:
            stats["bad_ts"] += 1
            continue
        if parse_ts(raw_ts) is None:
            stats["naive_ts"] += 1          # ISO senza fuso: assunto UTC
        sh, sa = r["score_home"], r["score_away"]
        if sh is None or sa is None:
            stats["bad_score"] += 1
            continue
        try:
            sh, sa = int(sh), int(sa)
        except (TypeError, ValueError):
            stats["bad_score"] += 1
            continue
        home = str(r["home_team"] or "")
        away = str(r["away_team"] or "")
        if not _norm_key(home) or not _norm_key(away) or _norm_key(home) == _norm_key(away):
            stats["bad_names"] += 1
            continue
        history.append({"match_id": r["match_id"], "league": r["league"] or "",
                        "home_team": home, "away_team": away,
                        "score_home": sh, "score_away": sa,
                        "result": (r["result"] or "").strip().upper(),
                        "ts": ts})
        stats["rows_used"] += 1
        if limit and stats["rows_used"] >= int(limit):
            break
    return TeamIndex(history, allow_alias=allow_alias), stats


# ---------------------------------------------------------------------------
# Stato CRONOLOGICO: Elo e contesto di lega PRIMA del kickoff
# ---------------------------------------------------------------------------

class ChronoState:
    """Elo delle squadre + statistiche di lega nello stato PRE-partita.

    Perche' uno STATO e non una funzione pura: l'Elo di una partita dipende da
    tutte quelle precedenti, e l'unico modo di calcolarlo senza look-ahead e'
    scorrere la storia UNA VOLTA in ordine cronologico leggendo il rating PRIMA
    di aggiornarlo col risultato. Ricostruirlo a ogni riga sarebbe O(n^2) su
    17k partite (e le medie di lega sono incrementali per costruzione).

    `snapshot()` e' PURO (non aggiorna nulla): le due meta' — leggere prima,
    aggiornare dopo — restano separate, cosi' una riga non puo' vedere il
    proprio risultato nemmeno per sbaglio.
    """

    def __init__(self, *, home_advantage: float = ELO_HOME_ADVANTAGE,
                 k: float = ELO_K) -> None:
        self.elo: Dict[str, float] = {}
        self.league_matches: Dict[str, int] = {}
        self.league_goals: Dict[str, int] = {}
        self.home_advantage = float(home_advantage)
        self.k = float(k)
        self.observed = 0

    def rating(self, team: Any) -> float:
        """Elo attuale (squadra mai vista -> `ELO_BASE`, il prior neutro)."""
        return float(self.elo.get(_norm_key(team), ELO_BASE))

    def known(self, team: Any) -> bool:
        key = _norm_key(team)
        return bool(key) and key in self.elo

    def snapshot(self, home: Any, away: Any, league: str = "") -> Dict[str, Any]:
        """Valori PRE-partita (non aggiorna nulla).

        `elo_diff` e' il divario AGGIUSTATO col vantaggio campo (casa + 65 di
        Elo), cioe' la quantita' da cui esce `elo_expect_home`: un divario
        grezzo senza vantaggio campo sarebbe meno informativo.
        `league_goal_avg`/`league_prev_matches` sono `None` senza precedenti di
        quella lega: 0 sarebbe un dato inventato (regola del modulo).
        """
        rh, ra = self.rating(home), self.rating(away)
        lk = _norm_key(league)
        n = int(self.league_matches.get(lk, 0)) if lk else 0
        out: Dict[str, Any] = {
            "elo_home": rh,
            "elo_away": ra,
            "elo_diff": rh + self.home_advantage - ra,
            "elo_expect_home": elo_expected(rh, ra,
                                            home_advantage=self.home_advantage),
            "league_goal_avg": (self.league_goals[lk] / n) if n else None,
            "league_prev_matches": n if n else None,
        }
        return out

    def observe(self, home: Any, away: Any, score_home: int, score_away: int,
                league: str = "") -> None:
        """Aggiorna lo stato col risultato (Elo + medie di lega).

        Una riga con nomi illeggibili non aggiorna nulla: meglio uno stato piu'
        povero che un rating attribuito alla squadra sbagliata.
        """
        hk, ak = _norm_key(home), _norm_key(away)
        if not hk or not ak or hk == ak:
            return
        try:
            sh, sa = int(score_home), int(score_away)
        except (TypeError, ValueError):
            return
        if sh < 0 or sa < 0:
            return
        nh, na = elo_update(self.elo.get(hk, ELO_BASE), self.elo.get(ak, ELO_BASE),
                            sh, sa, k=self.k, home_advantage=self.home_advantage)
        self.elo[hk] = nh
        self.elo[ak] = na
        lk = _norm_key(league)
        if lk:
            self.league_matches[lk] = int(self.league_matches.get(lk, 0)) + 1
            self.league_goals[lk] = int(self.league_goals.get(lk, 0)) + sh + sa
        self.observed += 1


def _chrono_rows(conn: sqlite3.Connection
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Righe storiche in ordine CRONOLOGICO (sola lettura, fail-safe) + scarti.

    Gli scarti sono CONTATI e restituiti: "stato costruito su 17k righe" e
    "stato costruito su 200 righe perche' il resto era illeggibile" sono due
    diagnosi diverse, e senza il conteggio sarebbero indistinguibili.
    """
    stats = {"rows": 0, "rows_used": 0, "bad_ts": 0,
             "bad_score": 0, "bad_names": 0}
    try:
        raw = conn.execute(
            "SELECT match_id, league, home_team, away_team, score_home, "
            "score_away, settled_at FROM match_results").fetchall()
    except sqlite3.Error as exc:
        logger.warning("match_features: storia per lo stato non leggibile (%s)", exc)
        return [], stats
    out: List[Dict[str, Any]] = []
    for r in raw:
        stats["rows"] += 1
        ts = _epoch(r["settled_at"], assume_utc=True)
        if ts is None:
            stats["bad_ts"] += 1
            continue
        try:
            sh, sa = int(r["score_home"]), int(r["score_away"])
        except (TypeError, ValueError):
            stats["bad_score"] += 1
            continue
        home, away = str(r["home_team"] or ""), str(r["away_team"] or "")
        if not _norm_key(home) or not _norm_key(away) or \
                _norm_key(home) == _norm_key(away):
            stats["bad_names"] += 1
            continue
        out.append({"match_id": r["match_id"], "league": str(r["league"] or ""),
                    "home_team": home, "away_team": away,
                    "score_home": sh, "score_away": sa, "ts": float(ts)})
        stats["rows_used"] += 1
    out.sort(key=lambda x: x["ts"])
    return out, stats


def build_state(conn: sqlite3.Connection, *,
                until: Optional[float] = None) -> ChronoState:
    """Stato caldo fino a `until` (esclusa): per il percorso LIVE di una partita.

    Senza `until` lo stato assorbe tutta la storia (l'ultimo istante noto); con
    `until` assorbe solo cio' che precede il kickoff, che e' l'unica cosa che
    una previsione puo' sapere.
    """
    state = ChronoState()
    rows, _stats = _chrono_rows(conn)
    for row in rows:
        if until is not None and float(row["ts"]) >= float(until):
            break
        state.observe(row["home_team"], row["away_team"],
                      row["score_home"], row["score_away"], row["league"])
    return state


# ---------------------------------------------------------------------------
# Le feature
# ---------------------------------------------------------------------------

def _team_block(index: TeamIndex, team: Any, kickoff_epoch: float, *,
                form_n: int = FORM_N) -> Dict[str, Any]:
    """Feature di UNA squadra prima del kickoff (riposo, densita', forma)."""
    out: Dict[str, Any] = {
        "hist_matches": 0, "rest_days": None,
        "matches_7d": 0, "matches_14d": 0, "matches_28d": 0,
        "congested": 0, "short_rest": 0,
        "pts5": None, "gf5": None, "ga5": None,
    }
    prior = index.matches_before(team, kickoff_epoch)
    out["hist_matches"] = len(prior)
    if not prior:
        # Nessuna storia nota: le feature restano ASSENTI (`None`), non 0 — uno
        # 0 vero ("ha giocato 0 partite negli ultimi 7 giorni") e uno 0
        # inventato ("non so chi sia questa squadra") sono cose diverse.
        return out
    last_ts = prior[-1][0]
    rest_days = (kickoff_epoch - last_ts) / 86400.0
    # Un doppio turno nello stesso giorno (0 giorni di riposo) e' un caso reale:
    # `max(0.0, ...)` evita un valore negativo se l'ordine dei dati e' strano.
    out["rest_days"] = round(max(0.0, rest_days), 3)
    for window in REST_WINDOWS:
        cutoff = kickoff_epoch - window * 86400.0
        out[f"matches_{window}d"] = sum(1 for e in prior if e[0] >= cutoff)
    out["congested"] = 1 if out["matches_7d"] >= CONGESTED_MATCHES_7D else 0
    out["short_rest"] = 1 if out["rest_days"] <= SHORT_REST_DAYS else 0
    recent = prior[-form_n:]
    pts = gf = ga = 0
    for _ts, _mid, f, a, _home in recent:
        gf += f
        ga += a
        pts += 3 if f > a else (1 if f == a else 0)
    n = len(recent)
    out["pts5"] = round(pts / n, 4)
    out["gf5"] = round(gf / n, 4)
    out["ga5"] = round(ga / n, 4)
    return out


def features_for(home: Any, away: Any, kickoff: Any, *,
                 index: TeamIndex,
                 xg_fn: Optional[Callable[[str, str], Any]] = None,
                 league: str = "",
                 state: Optional[ChronoState] = None,
                 assume_utc: bool = True) -> Dict[str, Any]:
    """Riga di feature completa per una partita (nessun look-ahead).

    `xg_fn(team, league)` e' iniettabile e **spento di default**: senza di esso
    le feature xG restano `None` e `xg_present` = 0 (in una diagnostica offline
    non deve partire nessuna rete).

    `state` e' lo stato cronologico (Elo + contesto di lega): senza di esso
    quelle feature restano `None` — NON si inventa un Elo di base per una
    partita di cui non conosciamo la storia (il percorso LIVE che vuole l'Elo
    lo costruisce con `build_state(conn, until=kickoff)`, che e' caldo ma
    sempre precedente al kickoff).
    """
    ts = _epoch(kickoff, assume_utc=assume_utc)
    row: Dict[str, Any] = {n: None for n in FEATURE_NAMES}
    if ts is None:
        return row
    if state is not None:
        row.update(state.snapshot(home, away, league))
    for side, team in (("home", home), ("away", away)):
        block = _team_block(index, team, ts)
        for name, value in block.items():
            row[f"{name}_{side}"] = value
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    row["is_weekend"] = 1 if dt.weekday() >= 5 else 0
    row["kickoff_hour"] = dt.hour
    row["month"] = dt.month
    # `xg_present` e' un FLAG, non una misura: vale 0 anche quando l'xG non e'
    # stato chiesto (cosi' il consumatore distingue "xG assente" da "xG non
    # richiesto" leggendo `xg_for_home`, che resta `None`).
    row["xg_present"] = 0
    if xg_fn is not None:
        present = 0
        for side, team in (("home", home), ("away", away)):
            try:
                stats = xg_fn(str(team), league)
            except Exception as exc:                 # un provider rotto non nega il resto
                logger.debug("match_features: xg_fn ha fallito per %s: %s", team, exc)
                stats = None
            if stats is None:
                continue
            xf = getattr(stats, "xg_for", None)
            xa = getattr(stats, "xg_against", None)
            row[f"xg_for_{side}"] = xf
            row[f"xg_against_{side}"] = xa
            if xf is not None or xa is not None:
                present = 1
        row["xg_present"] = present
    return row


def live_xg_fn(team: str, league: str):
    """Adapter xG reale (`live_intel`, FBref) — usato SOLO su richiesta.

    Import pigro e fail-safe: se il provider e' offline la feature resta
    assente (`None`), mai un valore inventato.
    """
    try:
        from live_intel import soccer_team_stats
        return soccer_team_stats(team, league)
    except Exception as exc:
        logger.debug("match_features: live_intel non disponibile: %s", exc)
        return None


def feature_vector(row: Dict[str, Any], names: Sequence[str] = FEATURE_NAMES
                   ) -> List[Optional[float]]:
    """Vettore nell'ordine di `names`, con i buchi **preservati** (`None`).

    Non si riempie nulla: scegliere come trattare un dato mancante e' una
    decisione del consumatore (e va dichiarata), non un default nascosto qui.
    """
    out: List[Optional[float]] = []
    for name in names:
        value = row.get(name)
        if value is None:
            out.append(None)
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Popolazione STORICA (per la scrematura) — senza look-ahead
# ---------------------------------------------------------------------------

def _label(sh: int, sa: int, target: str) -> Optional[int]:
    total = sh + sa
    if target == "home_win":
        return 1 if sh > sa else 0
    if target == "home_or_draw":
        return 1 if sh >= sa else 0
    if target == "away_win":
        return 1 if sa > sh else 0
    if target == "over25":
        return 1 if total > 2.5 else 0
    return None


def history_feature_rows(conn: Optional[sqlite3.Connection] = None, *,
                         limit: Optional[int] = None,
                         target: str = "home_win",
                         allow_alias: bool = True
                         ) -> Dict[str, Any]:
    """Righe (feature + label) dalla storia, OGNI riga senza look-ahead.

    Il risultato della partita e' escluso dal proprio indice per costruzione
    (`ts < kickoff` stretto), quindi la riga non vede ne' se stessa ne' il
    futuro. `stats` dichiara quante righe sono state scartate e perche'.
    """
    out: Dict[str, Any] = {"rows": [], "error": None, "stats": {}, "target": target}
    if target not in TARGETS:
        out["error"] = f"target_non_valido:{target}"
        return out
    own = conn is None
    try:
        if own:
            conn = ro_conn()
        index, stats = build_index(conn)
        chrono, chrono_stats = _chrono_rows(conn)
        # Passata CRONOLOGICA UNICA: si legge lo stato PRIMA e si aggiorna DOPO,
        # quindi nessuna riga vede se stessa ne' il futuro.
        cap = int(limit) if limit else None
        # Con un limite si EMETTONO le partite piu' recenti, ma lo stato (Elo,
        # medie di lega) si costruisce su TUTTA la storia: un Elo "freddo" sulle
        # righe iniziali sarebbe una feature piu' povera, non piu' corretta.
        first = 0 if cap is None else max(0, len(chrono) - cap)
        state = ChronoState()
        rows: List[Dict[str, Any]] = []
        skipped = {"unknown_team": 0, "cold_elo": 0}
        for i, r in enumerate(chrono):
            ts = float(r["ts"])
            home, away = r["home_team"], r["away_team"]
            league = str(r["league"] or "")
            if i >= first:
                feat = features_for(home, away, ts, index=index,
                                    league=league, state=state)
                if not feat.get("hist_matches_home") and \
                        not feat.get("hist_matches_away"):
                    # Nessuna delle due squadre ha storia: la riga non porta
                    # informazione (tutte le feature di storia sono assenti).
                    skipped["unknown_team"] += 1
                else:
                    if not (state.known(home) and state.known(away)):
                        # Almeno una squadra e' alla PRIMA partita osservata: il
                        # suo Elo e' il prior, non una misura (dichiarato).
                        skipped["cold_elo"] += 1
                    feat["match_id"] = r["match_id"]
                    feat["league"] = league
                    feat["ts"] = ts          # ordine CRONOLOGICO (walk-forward)
                    feat["label"] = _label(r["score_home"], r["score_away"], target)
                    feat["result"] = ("1" if r["score_home"] > r["score_away"]
                                      else ("2" if r["score_home"] < r["score_away"]
                                            else "X"))
                    rows.append(feat)
            state.observe(home, away, r["score_home"], r["score_away"], league)
        if not chrono and stats.get("rows"):
            out["error"] = "storia_senza_righe_usabili"
            return out
        out["rows"] = rows
        # UN solo posto per gli scarti (gli stessi contatori di prima + i nuovi):
        # "quante righe ho perso e perche'" non deve richiedere due letture.
        skipped_extra = {"bad_ts": chrono_stats["bad_ts"],
                         "bad_score": chrono_stats["bad_score"],
                         "bad_names": chrono_stats["bad_names"]}
        out["stats"] = {**stats, **chrono_stats, "feature_rows": len(rows),
                        "elo_observed": state.observed,
                        "elo_teams": len(state.elo),
                        "leagues": len(state.league_matches),
                        "skipped": {**skipped, **skipped_extra}}
        return out
    except Exception as exc:                          # fail-safe totale
        out["error"] = f"errore:{type(exc).__name__}:{exc}"
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scrematura statistica (AUC di Mann-Whitney, numpy-free per le parti critiche)
# ---------------------------------------------------------------------------

def _average_ranks(values: Sequence[float]) -> List[float]:
    """Ranghi medi (1-based) con gestione corretta dei pareggi."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def feature_auc(rows: Sequence[Dict[str, Any]], name: str, *,
                target: str = "home_win") -> Dict[str, Any]:
    """AUC della feature vs la label + test di Mann-Whitney (senza scipy).

    AUC = P(feature piu' alta in una partita vinta che in una persa). 0.5 =
    nessuna informazione, 1.0/-0.0 = separazione perfetta. Il p-value e' la
    normale con correzione per i pareggi: con campioni da migliaia di righe e'
    la lettura onesta (una feature con AUC 0.52 e p=1e-6 porta pochissima
    informazione ma non e' rumore).
    """
    values: List[float] = []
    labels: List[int] = []
    for r in rows:
        lab = r.get("label")
        if lab is None:
            continue
        val = r.get(name)
        if val is None:
            continue
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            continue
        labels.append(int(lab))
    n = len(values)
    n_pos = sum(1 for l in labels if l == 1)
    n_neg = n - n_pos
    row: Dict[str, Any] = {"name": name, "n": n, "n_pos": n_pos, "n_neg": n_neg,
                           "auc": None, "z": None, "p": None, "useful": False,
                           "reason": ""}
    if n == 0:
        # "dato assente" e "classi sbilanciate" sono diagnosi DIVERSE: la prima
        # dice che la feature non e' stata calcolata (o il DB e' vuoto), la
        # seconda che il campione non ha entrambi gli esiti. Confonderle manda
        # a cercare il problema nella direzione sbagliata.
        row["reason"] = "nessun_dato"
        return row
    if n_pos == 0 or n_neg == 0:
        row["reason"] = "una_sola_classe"
        return row
    ranks = _average_ranks(values)
    rank_pos = sum(rk for rk, l in zip(ranks, labels) if l == 1)
    u = rank_pos - n_pos * (n_pos + 1) / 2.0
    auc = u / (n_pos * n_neg)
    # Deviazione standard dell'U con correzione per i PAREGGI: senza, le
    # feature a valori discreti (rest_days, congested) avrebbero un p-value
    # ottimistico e sembrerebbero piu' informative di quanto siano.
    counts: Dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    tie_sum = sum(c ** 3 - c for c in counts.values())
    sigma_sq = (n_pos * n_neg / 12.0) * ((n + 1) - tie_sum / (n * (n - 1)))
    z = (u - n_pos * n_neg / 2.0) / math.sqrt(sigma_sq) if sigma_sq > 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2.0))
    row.update({"auc": auc, "z": z, "p": p})
    row["useful"] = bool(abs(auc - 0.5) >= AUC_EPS and p < P_MAX)
    if not row["useful"]:
        row["reason"] = ("effetto_trascurabile" if abs(auc - 0.5) < AUC_EPS
                         else "non_significativo")
    return row


def screening(rows: Sequence[Dict[str, Any]], *, target: str = "home_win",
              names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Scrematura di tutte le feature usabili senza look-ahead.

    Restituisce i risultati ordinati per |AUC - 0.5| (le piu' informative
    prima) e DICHIARA: il campione, le feature escluse per look-ahead e le
    feature che il campione non puo' giudicare (`insufficient`).
    """
    names = tuple(names) if names is not None else HISTORY_FEATURES
    out: Dict[str, Any] = {
        "target": target, "n": len(rows), "results": [],
        "excluded_lookahead": list(LOOKAHEAD_UNSAFE),
        "sufficient": len(rows) >= MIN_LABELS, "error": None,
    }
    if target not in TARGETS:
        out["error"] = f"target_non_valido:{target}"
        return out
    if not out["sufficient"]:
        out["note"] = (f"campione {len(rows)} < {MIN_LABELS}: l'AUC e' rumore, "
                       "non si conclude nulla")
    results = [feature_auc(rows, name, target=target) for name in names]
    results.sort(key=lambda r: (-(abs((r["auc"] or 0.5) - 0.5))), )
    out["results"] = results
    out["useful"] = [r["name"] for r in results if r["useful"]]
    return out


def coverage(rows: Sequence[Dict[str, Any]],
             names: Sequence[str] = FEATURE_NAMES) -> Dict[str, float]:
    """Quota di righe in cui ogni feature e' PRESENTE (mai contata come 0)."""
    n = len(rows)
    if not n:
        return {name: 0.0 for name in names}
    return {name: sum(1 for r in rows if r.get(name) is not None) / n
            for name in names}


# ---------------------------------------------------------------------------
# Report / CLI
# ---------------------------------------------------------------------------

def format_report(data: Dict[str, Any]) -> str:
    """Report testuale (Telegram-friendly) della scrematura + copertura."""
    lines: List[str] = []
    if data.get("error"):
        return f"❌ match_features: {data['error']}"
    stats = data.get("stats") or {}
    lines.append(f"🧪 FEATURE SCREENING — target {data.get('target')}")
    lines.append(f"Campione: {data.get('n')} partite "
                 f"(righe storia usate {stats.get('feature_rows', '?')}, "
                 f"scartate {stats.get('skipped', {})})")
    if not data.get("sufficient"):
        lines.append(f"⚠️ {data.get('note', 'campione insufficiente')}")
    for row in data.get("results", [])[:12]:
        auc = row.get("auc")
        if auc is None:
            lines.append(f"· {row['name']:<22} n={row['n']:<6} — {row['reason']}")
            continue
        marker = "✅" if row["useful"] else "  "
        lines.append(f"{marker} {row['name']:<22} AUC {auc:.4f} "
                     f"p={row['p']:.2e} n={row['n']}"
                     + ("" if row["useful"] else f" ({row['reason']})"))
    if data.get("excluded_lookahead"):
        lines.append("Escluse (look-ahead sulla storia): "
                     + ", ".join(data["excluded_lookahead"]))
    cov = data.get("coverage")
    if cov:
        thin = sorted(cov.items(), key=lambda kv: kv[1])[:6]
        lines.append("Copertura piu' bassa: "
                     + ", ".join(f"{k} {v:.0%}" for k, v in thin))
    lines.append("Nessuna decisione: la scrematura serve a PESCARE le feature "
                 "prima di addestrare (porta di uscita: Brier walk-forward).")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:      # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Feature engineering gratuito per il modello ML (modulo 1)")
    ap.add_argument("--db", default=None, help="ledger (default tracker.DB_PATH)")
    ap.add_argument("--history", action="store_true",
                    help="costruisce le righe storiche con le feature")
    ap.add_argument("--screening", action="store_true",
                    help="scrematura AUC delle feature (richiede --history)")
    ap.add_argument("--coverage", action="store_true",
                    help="copertura delle feature (quanto spesso sono presenti)")
    ap.add_argument("--target", default="home_win", choices=list(TARGETS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--match", nargs=2, metavar=("HOME", "AWAY"), default=None,
                    help="feature di una singola partita")
    ap.add_argument("--kickoff", default=None, help="kickoff ISO (con --match)")
    ap.add_argument("--league", default="", help="lega (con --match)")
    ap.add_argument("--xg", action="store_true",
                    help="interroga live_intel per l'xG (rete: solo su richiesta)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    payload: Dict[str, Any] = {}
    if args.match:
        kickoff = args.kickoff or datetime.now(timezone.utc).isoformat()
        conn = ro_conn(args.db)
        try:
            index, stats = build_index(conn)
            # Stato caldo ma SOLO con cio' che precede il kickoff: il percorso
            # live ha anche l'Elo e il contesto di lega, senza look-ahead.
            state = build_state(conn, until=_epoch(kickoff, assume_utc=True))
        finally:
            conn.close()
        feat = features_for(args.match[0], args.match[1], kickoff, index=index,
                            xg_fn=live_xg_fn if args.xg else None,
                            league=args.league, state=state)
        payload = {"mode": "match", "kickoff": kickoff, "features": feat,
                   "stats": stats}
        if not args.json:
            print(f"🧪 FEATURE — {args.match[0]} vs {args.match[1]} @ {kickoff}")
            for name in FEATURE_NAMES:
                print(f"  {name:<24} {feat.get(name)}")
            return 0
    else:
        conn = ro_conn(args.db)
        try:
            data = history_feature_rows(conn, limit=args.limit,
                                        target=args.target)
        finally:
            conn.close()
        if data.get("error"):
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 1
        rows = data["rows"]
        payload = {"mode": "history", **data}
        payload["n"] = len(rows)
        payload["coverage"] = coverage(rows)
        if args.screening:
            payload["screening"] = screening(rows, target=args.target)
            payload["results"] = payload["screening"]["results"]
            payload["sufficient"] = payload["screening"]["sufficient"]
            payload["excluded_lookahead"] = payload["screening"]["excluded_lookahead"]
            if payload["screening"].get("note"):
                payload["note"] = payload["screening"]["note"]
        if not args.json:
            print(format_report(payload))
            return 0
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
