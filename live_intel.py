"""Intel live a costo zero per il Data Agent (direttiva 29/09/2026).

FOCUS INTEL (il modulo NON decide, NON ordina, NON scrive sul ledger):
- statistiche di stagione (xG per 90, xGA, gol fatti/subiti) — soccerdata/FBref;
- rating ELO — soccerdata/ClubElo;
- news last-minute su infortuni/formazioni — ddgs (DuckDuckGo, zero API key);
- NBA: statistiche squadra di stagione — nba_api (NBA Stats API);
- MLB: probabili lanciatori titolari — endpoint pubblico MLB StatsAPI
  (lo stesso che incapsula pybaseball; la versione installata 2.2.7 non
  espone ``probable_starters`` e pybaseball NON e' invocabile fuori dal
  baseball: l'adapter solleva OUT_OF_SCOPE, mai un numero inventato).

GARANZIE (tripwire in test_live_intel.py):
- nessun ordine (`place_limit_order`/`execution_engine`/`_live_fill`/`save_bet`
  compaiono SOLO in questo docstring, mai nel codice);
- ogni chiamata a un provider gira sotto una guardia di TIMEOUT
  (`_network_deadline`, env `LIVE_INTEL_TIMEOUT_S`): le librerie di scraping
  non espongono un timeout e senza guardia bloccano il ciclo a tempo
  indefinito (misurato il 29/09/2026);
- import PIGRI: importare `live_intel` non carica nessuna libreria pesante
  (le librerie entrano SOLO dentro le funzioni degli adapter);
- fail-safe per ADAPTER: un provider rotto/offline non nega mai l'intel
  complessiva (gli altri provider lavorano) e non solleva mai verso il
  chiamante (``assemble_match_intel`` ritorna il modello con gli errori
  DENTRO, contati);
- nessuna soglia di strategia: l'EV resta quello dei moduli esistenti
  (`decision`/`auto_bet`), qui non si ricalcola nulla.

Le librerire sono gratuite ma NON a costo zero in TEMPO (scraping FBref,
rate limit NBA): per questo ogni lettura passa da una CACHE SU DISCO con
TTL per provider (env `LIVE_INTEL_TTL_*`): il ciclo del Chief puo'
arricchire i segnali senza martellare le fonti.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = [
    "MatchIntel", "NewsItem", "TeamStats", "ProviderStatus",
    "assemble_match_intel", "collect_news", "soccer_team_stats",
    "soccer_elo", "mlb_probable_pitchers", "nba_team_stats",
    "INTEL_ERRORS", "reset_cache_dir",
]


# ---------------------------------------------------------------------------
# Stato / env
# ---------------------------------------------------------------------------

#: Errori dell'ultimo giro (telemetria: "provider rotto" non deve leggersi
#: come "nessun problema"). Chiave = provider, valore = motivo machine-readable.
INTEL_ERRORS: dict[str, str] = {}

_CACHE_DIR: Optional[Path] = None  # override esplicito (test / env)


def _cache_dir() -> Path:
    """Cartella cache: sotto `DATA_DIR` del progetto (volume persistente).

    Un override esplicito (`reset_cache_dir` / env `LIVE_INTEL_CACHE`)
    vince SEMPRE: i test devono poter isolare la cache dal volume.
    """
    if _CACHE_DIR is not None:
        return _CACHE_DIR
    env = os.getenv("LIVE_INTEL_CACHE")
    if env:
        return Path(env)
    try:
        from config import DATA_DIR  # import pigro: `import live_intel` resta leggero
        return Path(DATA_DIR) / "intel"
    except Exception:
        return Path("data/intel")


def reset_cache_dir(path: Optional[str | Path]) -> None:
    """Forza la cartella cache (uso nei test: isolamento dal volume).

    `None` ripristina il default (DATA_DIR/intel).
    """
    global _CACHE_DIR
    _CACHE_DIR = Path(path) if path is not None else None


def _ttl(provider: str, default_h: float) -> float:
    raw = os.getenv(f"LIVE_INTEL_TTL_{provider.upper()}", "")
    try:
        val = float(raw)
        return max(val, 0.05)
    except (TypeError, ValueError):
        return default_h


#: Budget di rete di DEFAULT per singola chiamata a un provider (secondi).
DEFAULT_TIMEOUT_S = 20.0


def _timeout_seconds() -> float:
    """Budget di rete per chiamata (env `LIVE_INTEL_TIMEOUT_S`, default 20s).

    Un valore non numerico o <= 0 ricade sul default: una guardia di sicurezza
    non deve poter essere spenta da un env sbagliato.
    """
    raw = os.getenv("LIVE_INTEL_TIMEOUT_S", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return val if val > 0 else DEFAULT_TIMEOUT_S


@contextlib.contextmanager
def _network_deadline():
    """Limita la durata delle chiamate che NON accettano un timeout.

    `soccerdata` (FBref/ClubElo) e `ddgs` non espongono un parametro di
    timeout: senza guardia una fonte lenta blocca il ciclo (e la suite di
    test) a tempo indefinito — misurato il 29/09/2026, quando lo scraping
    FBref ha bloccato l'intera regressione. La guardia agisce sul timeout di
    DEFAULT dei socket: e' l'unico punto che urllib3/requests rispettano
    quando il chiamante non passa un timeout esplicito.

    Il valore precedente viene SEMPRE ripristinato (nessun effetto residuo
    sul processo, che ospita anche lo scheduler del bot).
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_timeout_seconds())
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def _cache_read(kind: str, key: str) -> Optional[dict]:
    """Legge la cache di un provider (dict `{ts, data}`), mai un'eccezione."""
    try:
        path = _cache_dir() / f"{kind}_{_slug(key)}.json"
        if not path.exists():
            return None
        blob = json.loads(path.read_text(encoding="utf-8"))
        ts = float(blob.get("ts") or 0.0)
        age_h = (datetime.now(timezone.utc).timestamp() - ts) / 3600.0
        if age_h > _ttl(kind.split("_", 1)[0], 6.0):
            return None
        return blob
    except Exception:
        return None


def _cache_write(kind: str, key: str, data: Any) -> None:
    """Scrive la cache (best-effort: un disco pieno non rompe l'intel)."""
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{kind}_{_slug(key)}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "ts": datetime.now(timezone.utc).timestamp(),
            "data": data,
        }, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _slug(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Contratti Pydantic (l'output del Data Agent, non dict anonimi)
# ---------------------------------------------------------------------------

class NewsItem(BaseModel):
    """Una notizia (titolo + link), mai il corpo: e' un PUNTATORE alla fonte."""
    title: str
    url: str = ""
    source: str = ""
    published: str = ""

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TeamStats(BaseModel):
    """Statistiche di stagione di una squadra (dal provider disponibile)."""
    provider: str = ""
    xg_for: Optional[float] = None          # xG per 90 (FBref/Understat)
    xg_against: Optional[float] = None
    goals_for: Optional[float] = None
    goals_against: Optional[float] = None
    matches: Optional[int] = None
    elo: Optional[float] = None             # ClubElo (calcio) / rating NBA

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ProviderStatus(BaseModel):
    """Stato di un provider nel giro (visibilita' totale, mai silenzio)."""
    provider: str
    ok: bool = False
    detail: str = ""      # "ok", "not_applicable", "offline", "error: ..."

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class MatchIntel(BaseModel):
    """Quadro intel di UN match (aggregato dei provider).

    NON contiene EV ne' verdetti: sono cose dei moduli di strategia. Qui
    ci sono i FATTI (statistiche, assenze segnalate dalle news) e lo stato
    dei provider che li hanno prodotti.
    """
    match_id: str = ""
    home: str = ""
    away: str = ""
    league: str = ""
    home_stats: Optional[TeamStats] = None
    away_stats: Optional[TeamStats] = None
    injuries_news: list[NewsItem] = Field(default_factory=list)
    probable_pitchers: dict[str, str] = Field(default_factory=dict)
    providers: list[ProviderStatus] = Field(default_factory=list)

    @property
    def errors(self) -> int:
        """Numero di provider CRASHATI (fonte unica: la lista provider)."""
        return sum(1 for p in self.providers
                   if p.detail.startswith("error:"))

    @property
    def partial(self) -> bool:
        """True se ALMENO un provider ha fallito (degrado dichiarato)."""
        return self.errors > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "home": self.home,
            "away": self.away,
            "league": self.league,
            "home_stats": self.home_stats.as_json() if self.home_stats else None,
            "away_stats": self.away_stats.as_json() if self.away_stats else None,
            "injuries_news": [n.as_json() for n in self.injuries_news],
            "probable_pitchers": self.probable_pitchers,
            "providers": [p.as_json() for p in self.providers],
            "errors": self.errors,
            "partial": self.partial,
        }


# ---------------------------------------------------------------------------
# Nomi dei campionati -> chiavi soccerdata (tabella esplicita, mai fuzzy)
# ---------------------------------------------------------------------------

_SD_LEAGUES = {
    "england premier league": ("ENG-Premier League", "ENG_Premier_League"),
    "premier league": ("ENG-Premier League", "ENG_Premier_League"),
    "efl championship": ("ENG-Championship", "ENG_Championship"),
    "championship": ("ENG-Championship", "ENG_Championship"),
    "italy serie a": ("ITA-Serie A", "ITA_Serie_A"),
    "serie a": ("ITA-Serie A", "ITA_Serie_A"),
    "spain la liga": ("ESP-La Liga", "ESP_La_Liga"),
    "la liga": ("ESP-La Liga", "ESP_La_Liga"),
    "germany bundesliga": ("GER-Bundesliga", "GER_Bundesliga"),
    "bundesliga": ("GER-Bundesliga", "GER_Bundesliga"),
    "france ligue 1": ("FRA-Ligue 1", "FRA_Ligue_1"),
    "ligue 1": ("FRA-Ligue 1", "FRA_Ligue_1"),
    "netherlands eredivisie": ("NED-Eredivisie", "NED_Eredivisie"),
    "eredivisie": ("NED-Eredivisie", "NED_Eredivisie"),
    "portugal primeira liga": ("POR-Primeira Liga", "POR_Primeira_Liga"),
    "turkey super lig": ("TUR-Super Lig", "TUR_Super_Lig"),
    "scotland premiership": ("SCO-Premiership", "SCO_Premiership"),
    "belgium first div": ("BEL-First Div A", "BEL_First_Div_A"),
    "mls": ("USA-Major League Soccer", "USA_Major_League_Soccer"),
    "liga mx": ("MEX-Liga MX", "MEX_Liga_MX"),
    "brazil serie a": ("BRA-Serie A", "BRA_Serie_A"),
    "brasileirao": ("BRA-Serie A", "BRA_Serie_A"),
}


def _sd_league(league: str) -> Optional[str]:
    """Chiave soccerdata dalla lega del ledger (None = non coperta)."""
    norm = re.sub(r"[^a-z0-9 ]", " ", (league or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    for alias, keys in _SD_LEAGUES.items():
        if norm == alias:
            return keys[0]
    return None


def _seasons() -> list[str]:
    """Stagioni correnti e precedente nel formato soccerdata (`2526`)."""
    now = datetime.now(timezone.utc)
    y = now.year if now.month >= 7 else now.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}",
            f"{(y - 1) % 100:02d}{y % 100:02d}"]


# ---------------------------------------------------------------------------
# PROVIDER: soccerdata (FBref stats + ClubElo)
# ---------------------------------------------------------------------------

def soccer_team_stats(team: str, league: str) -> Optional[TeamStats]:
    """xG/gol di stagione dal FBref via soccerdata. `None` se non coperto.

    Fail-safe: qualunque errore di scraping/rete -> None (mai un numero
    parziale inventato). La statistica arriva COMPLETA o non arriva.
    """
    sd_league = _sd_league(league)
    if not sd_league:
        return None
    key = f"fbref|{sd_league}|{team}"
    cached = _cache_read("fbref", key)
    if cached is not None:
        data = cached.get("data") or {}
        return TeamStats(provider="fbref", **data) if data else None
    try:
        import soccerdata as sd  # import pigro

        last_err = ""
        for season in _seasons():
            try:
                fb = sd.FBref(leagues=[sd_league], seasons=[season])
                df = fb.team_season_stats(stat_type="standard")
                if df is None or df.empty:
                    last_err = "dataset vuoto"
                    continue
                # In soccerdata l'ULTIMO livello d'indice e' la squadra.
                team_level = df.index.names[-1]
                if team not in df.index.get_level_values(-1):
                    last_err = "squadra non trovata"
                    continue
                row = df.xs(team, level=team_level).iloc[0]

                def _num(col: str) -> Optional[float]:
                    for c in df.columns:
                        if str(c[-1]).lower() == col:
                            v = row[c]
                            try:
                                return float(v)
                            except (TypeError, ValueError):
                                return None
                    return None

                def _int(col: str) -> Optional[int]:
                    v = _num(col)
                    return int(v) if v is not None else None

                stats = TeamStats(
                    provider="fbref",
                    xg_for=_num("xg"),
                    xg_against=_num("xga"),
                    goals_for=_num("goals"),
                    goals_against=_num("goals_against"),
                    matches=_int("matches"),
                )
                _cache_write("fbref", key, stats.model_dump(mode="json"))
                return stats
            except Exception as exc:  # stagione/giorno senza dati
                last_err = str(exc)[:120]
        INTEL_ERRORS["fbref"] = last_err or "nessuna stagione disponibile"
        return None
    except Exception as exc:
        INTEL_ERRORS["fbref"] = str(exc)[:120]
        return None


def soccer_elo(team: str) -> Optional[float]:
    """Rating ELO ClubElo (solo club europei: le grandi leghe)."""
    key = f"elo|{team}"
    cached = _cache_read("elo", key)
    if cached is not None and cached.get("data") is not None:
        return float(cached["data"])
    try:
        import soccerdata as sd  # import pigro

        elo = sd.ClubElo().read_by_date()  # snapshot corrente del rating
        # Il livello d'indice della squadra e' il primo (nome 'team' se
        # presente, altrimenti posizione 0: non dipendere dal nome esatto).
        lv = "team" if "team" in elo.index.names else 0
        if team not in elo.index.get_level_values(lv):
            INTEL_ERRORS["clubelo"] = f"squadra non coperta: {team}"
            _cache_write("elo", key, None)
            return None
        row = elo.xs(team, level=lv)
        value = float(row["elo"].iloc[0])
        _cache_write("elo", key, value)
        return value
    except Exception as exc:
        INTEL_ERRORS["clubelo"] = str(exc)[:120]
        return None


# ---------------------------------------------------------------------------
# PROVIDER: ddgs (news infortuni/formazioni, zero API key)
# ---------------------------------------------------------------------------

def collect_news(query: str, *, max_results: int = 5) -> list[NewsItem]:
    """News DuckDuckGo (pacchetto `ddgs`). Offline/bloccato -> [].

    DDG resetta le connessioni da IP datacenter: il fallimento e' NORMALE e
    non deve mai propagarsi (l'intel statistica resta valida senza news).
    """
    if not query:
        return []
    key = f"news|{query}"
    cached = _cache_read("news", key)
    if cached is not None:
        return [NewsItem(**n) for n in (cached.get("data") or [])]
    try:
        from ddgs import DDGS  # import pigro

        raw = list(DDGS().news(query, max_results=max_results))
        items = [NewsItem(
            title=str(r.get("title") or "")[:300],
            url=str(r.get("url") or r.get("href") or ""),
            source=str(r.get("source") or ""),
            published=str(r.get("date") or ""),
        ) for r in raw]
        items = [n for n in items if n.title]
        _cache_write("news", key, [n.model_dump(mode="json") for n in items])
        return items
    except Exception as exc:
        INTEL_ERRORS["ddgs"] = str(exc)[:120]
        return []


def _injuries_query(home: str, away: str, league: str) -> str:
    """Query di ricerca news orientata ad assenze last-minute."""
    lang = "infortuni" if _looks_italian(league) else "injury news"
    return f"{home} vs {away} {league} {lang} lineup".strip()


def _looks_italian(league: str) -> bool:
    return any(w in (league or "").lower()
               for w in ("serie a", "italy", "italia"))


# ---------------------------------------------------------------------------
# PROVIDER: MLB (probabili lanciatori) e NBA (statistiche squadra)
# ---------------------------------------------------------------------------

_MLB_STATS_API = "https://statsapi.mlb.com/api/v1"


def _mlb_team_id(team: str, games: list[dict]) -> Optional[int]:
    """Id MLB della squadra citata nella partita (sui game del giorno)."""
    norm = re.sub(r"[^a-z]", "", (team or "").lower())
    for g in games:
        for side in ("away", "home"):
            t = g.get("teams", {}).get(side, {}).get("team", {})
            name = re.sub(r"[^a-z]", "", (t.get("name") or "").lower())
            if norm and (norm in name or name in norm):
                return t.get("id")
    return None


def mlb_probable_pitchers(home: str, away: str) -> dict[str, str]:
    """Lanciatori titolari del giorno (MLB StatsAPI pubblico, zero chiavi).

    I probabili sono pubblicati il giorno stesso: fuori giorno di gara il
    dict resta vuoto (dato assente, mai stimato).
    """
    key = "mlb|probable"
    cached = _cache_read("mlb", key)
    if cached is not None:
        return {k: v for k, v in (cached.get("data") or {}).items() if v}
    try:
        import requests  # gia' nel progetto

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = requests.get(f"{_MLB_STATS_API}/schedule", params={
            "sportId": 1, "date": today, "hydrate": "probablePitcher",
        }, timeout=15)
        resp.raise_for_status()
        games = (resp.json() or {}).get("dates", [{}])[0].get("games", [])
        out: dict[str, str] = {}
        for team in (home, away):
            tid = _mlb_team_id(team, games)
            if not tid:
                continue
            for g in games:
                for side in ("away", "home"):
                    t = g.get("teams", {}).get(side, {}).get("team", {})
                    if t.get("id") != tid:
                        continue
                    p = g.get("teams", {}).get(side, {}).get("probablePitcher")
                    if p and p.get("fullName"):
                        out[team] = p["fullName"]
        _cache_write("mlb", key, out)
        if not out:
            INTEL_ERRORS["mlb"] = "nessun probabile pubblicato oggi"
        return out
    except Exception as exc:
        INTEL_ERRORS["mlb"] = str(exc)[:120]
        return {}


_NBA_TEAMS = {  # abbreviazione NBA Stats API (tabella esplicita)
    "boston celtics": "BOS", "brooklyn nets": "BKN", "new york knicks": "NYK",
    "philadelphia 76ers": "PHI", "toronto raptors": "TOR",
    "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "detroit pistons": "DET", "indiana pacers": "IND", "milwaukee bucks": "MIL",
    "atlanta hawks": "ATL", "charlotte hornets": "CHA", "miami heat": "MIA",
    "orlando magic": "ORL", "washington wizards": "WAS",
    "denver nuggets": "DEN", "minnesota timberwolves": "MIN",
    "oklahoma city thunder": "OKC", "portland trail blazers": "POR",
    "utah jazz": "UTA", "golden state warriors": "GSW",
    "los angeles clippers": "LAC", "los angeles lakers": "LAL",
    "phoenix suns": "PHX", "sacramento kings": "SAC",
    "dallas mavericks": "DAL", "houston rockets": "HOU",
    "memphis grizzlies": "MEM", "new orleans pelicans": "NOP",
    "san antonio spurs": "SAS",
}


def nba_team_stats(team: str) -> Optional[TeamStats]:
    """Statistiche squadra NBA (nba_api -> NBA Stats API)."""
    norm = re.sub(r"\s+", " ", (team or "").lower()).strip()
    abbr = _NBA_TEAMS.get(norm)
    if not abbr:
        return None  # NON e' un fallimento: il match non e' NBA
    key = f"nba|{abbr}"
    cached = _cache_read("nba", key)
    if cached is not None and cached.get("data"):
        return TeamStats(provider="nba_api", **cached["data"])
    try:
        from nba_api.stats.endpoints import leaguedashteamstats  # import pigro

        df = leaguedashteamstats.LeagueDashTeamStats(
            team_id_nullable="", season_nullable=None,
            season_type_nullable="Regular Season",
        ).get_data_frames()[0]
        row = df[df["TEAM_ABBREVIATION"] == abbr]
        if row.empty:
            INTEL_ERRORS["nba_api"] = f"squadra non trovata: {abbr}"
            return None
        r = row.iloc[0]
        stats = TeamStats(
            provider="nba_api",
            goals_for=float(r.get("PTS", 0) or 0),          # punti/partita
            goals_against=float(r.get("OPP_PTS", 0) or 0) if "OPP_PTS" in df.columns else None,
            matches=int(float(r.get("GP", 0) or 0)),
            elo=float(r.get("W_PCT", 0) or 0),              # win % (proxy dichiarato)
        )
        _cache_write("nba", key, stats.model_dump(mode="json"))
        return stats
    except Exception as exc:
        INTEL_ERRORS["nba_api"] = str(exc)[:120]
        return None


# ---------------------------------------------------------------------------
# AGGREGATORE
# ---------------------------------------------------------------------------

def _sport_of(league: str, home: str, away: str) -> str:
    l = (league or "").lower()
    if "nba" in l or l in _NBA_TEAMS or (home or "").lower() in _NBA_TEAMS \
            or (away or "").lower() in _NBA_TEAMS:
        return "nba"
    if "mlb" in l or "baseball" in l:
        return "mlb"
    return "soccer"


def assemble_match_intel(match: dict, *,
                         news_fetch: Optional[Callable[[str], list[NewsItem]]] = None,
                         stats_fn: Optional[Callable[[str, str], Optional[TeamStats]]] = None,
                         elo_fn: Optional[Callable[[str], Optional[float]]] = None,
                         mlb_fn: Optional[Callable[[str, str], dict]] = None,
                         nba_fn: Optional[Callable[[str], Optional[TeamStats]]] = None,
                         now: Optional[datetime] = None) -> MatchIntel:
    """Costruisce il `MatchIntel` di un match del ledger.

    Tutti i provider sono iniettabili (test offline). Qualunque errore resta
    DENTRO il modello (`providers[].detail`, `errors`): mai un'eccezione.
    """
    intel = MatchIntel(
        match_id=str(match.get("match_id") or match.get("id") or ""),
        home=str(match.get("home") or match.get("home_team") or ""),
        away=str(match.get("away") or match.get("away_team") or ""),
        league=str(match.get("league") or ""),
    )
    stats_fn = stats_fn or soccer_team_stats
    elo_fn = elo_fn or soccer_elo
    mlb_fn = mlb_fn or mlb_probable_pitchers
    nba_fn = nba_fn or nba_team_stats
    news_fetch = news_fetch or collect_news
    sport = _sport_of(intel.league, intel.home, intel.away)

    for status in _collect_providers(intel, sport, stats_fn, elo_fn,
                                     mlb_fn, nba_fn, news_fetch):
        intel.providers.append(status)
    return intel


def _collect_providers(intel: MatchIntel, sport: str,
                       stats_fn, elo_fn, mlb_fn, nba_fn,
                       news_fetch) -> list[ProviderStatus]:
    """Esegue i provider per lo sport del match, catturando tutto."""
    out: list[ProviderStatus] = []

    def _run(provider: str, fn: Callable[[], Any], applicable: bool,
             assign: Callable[[Any], None]) -> ProviderStatus:
        if not applicable:
            return ProviderStatus(provider=provider, ok=False,
                                  detail="not_applicable")
        try:
            with _network_deadline():  # una fonte lenta non blocca il ciclo
                value = fn()
            assign(value)
            ok = value is not None and value != {}
            return ProviderStatus(provider=provider, ok=ok,
                                  detail="ok" if ok else "senza dati")
        except Exception as exc:  # un provider non nega gli altri
            INTEL_ERRORS[provider] = str(exc)[:120]
            return ProviderStatus(provider=provider, ok=False,
                                  detail=f"error: {str(exc)[:100]}")

    if sport == "nba":
        out.append(_run(
            "nba_api", lambda: nba_fn(intel.home), True,
            lambda v: setattr(intel, "home_stats", v)))
        if intel.away and intel.away.lower() in _NBA_TEAMS:
            out.append(_run(
                "nba_api", lambda: nba_fn(intel.away), True,
                lambda v: setattr(intel, "away_stats", v)))
        return out

    if sport == "mlb":
        out.append(_run(
            "mlb", lambda: mlb_fn(intel.home, intel.away), True,
            lambda v: setattr(intel, "probable_pitchers", dict(v or {}))))
        return out

    # --- soccer: statistiche + ELO + news --------------------------------
    def _stats_with_elo(team: str) -> Optional[TeamStats]:
        stats = stats_fn(team, intel.league)
        if stats is None:
            elo = elo_fn(team)
            if elo is not None:
                stats = TeamStats(provider="clubelo", elo=elo)
        elif stats.elo is None:
            elo = elo_fn(team)
            if elo is not None:
                stats = stats.model_copy(update={"elo": elo})
        return stats

    out.append(_run("soccerdata", lambda: _stats_with_elo(intel.home), True,
                    lambda v: setattr(intel, "home_stats", v)))
    out.append(_run("soccerdata", lambda: _stats_with_elo(intel.away), True,
                    lambda v: setattr(intel, "away_stats", v)))
    out.append(_run(
        "ddgs",
        lambda: news_fetch(_injuries_query(intel.home, intel.away,
                                           intel.league)),
        True,
        lambda v: setattr(intel, "injuries_news", list(v or []))))
    return out


# ---------------------------------------------------------------------------
# CLI (diagnostica: `venv/bin/python live_intel.py "<squadra casa>" "<squadra trasferta>" --league "..."`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Intel live a costo zero")
    ap.add_argument("home", help="squadra casa")
    ap.add_argument("away", nargs="?", default="", help="squadra trasferta")
    ap.add_argument("--league", default="", help="lega (chiave del ledger)")
    args = ap.parse_args()
    print(json.dumps(assemble_match_intel({
        "match_id": "cli", "home": args.home, "away": args.away,
        "league": args.league,
    }).as_json(), indent=2, ensure_ascii=False))
