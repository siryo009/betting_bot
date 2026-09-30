import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from config import DATA_DIR
from odds_api import (fetch_odds, SPORTS_MAP, QUERY_WINDOW_DAYS,
                      interval_for_sport, is_sport_due, DAILY_QUERY_BUDGET)
from leagues_data import ALL_LEAGUES
from poisson_engine import expected_goals, prob_1x2, ah_outcome_probs
from value_filter import (compute_ev, kelly_fraction, kelly_euro, is_sane,
                           combined_quota, combined_probability, multipla_stake,
                           adjusted_probability, get_pro_stake,
                           eligible_favourites, favourites_gate_reason,
                           EV_MIN, EV_MAX, ODDS_MIN, ODDS_MAX, MARKET_EDGE_MIN)
from market_calib import market_implied, MARKET_EDGE_STRONG
from tracker import (save_match, get_today_matches, save_analysis, get_analysis_for_match,
                      clear_old_matches, save_clv, save_prediction)
from line_movement import record_snapshot, detect_rlm, detect_steam

logger = logging.getLogger(__name__)

# ML Ensemble: caricato lazy, addestrato quando il dataset matura
def _get_ensemble():
    try:
        from ml_ensemble import get_ensemble
        return get_ensemble()
    except ImportError:
        return None

# Sistema 1X2 SOLO (calcio) + 2-way (tennis). OU2.5 escluso definitivamente.
# Il backtest storico (12.909 partite) ha mostrato un leak sistematico
# (-6.8% ROI su 924 bet, sotto -7.3%): il mercato non viene piu' elaborato,
# nessuna escape hatch via env (decisione definitiva del proprietario).
# Il sistema elabora SOLO segnali 1X2 (h2h).
# Costante di blocco (tripwire test_ou_exclusion): nessun flag riconfigurabile.
OU_ENABLED = False

TEAM_MAP = {
    "inter milan": "Inter", "ac milan": "Milan", "man united": "Manchester United",
    "man utd": "Manchester United", "man city": "Manchester City",
    "tottenham hotspur": "Tottenham", "tottenham": "Tottenham",
    "athletic club": "Athletic Bilbao", "atletico madrid": "Atletico Madrid",
    "atletico": "Atletico Madrid", "paris saint-germain": "Paris Saint-Germain",
    "psg": "Paris Saint-Germain", "borussia dortmund": "Borussia Dortmund",
    "borussia mgladbach": "Borussia Mgladbach", "rb leipzig": "RB Leipzig",
    "bayern munich": "Bayern Munich", "bayer leverkusen": "Bayer Leverkusen",
    "eintracht frankfurt": "Eintracht Frankfurt", "west ham united": "West Ham",
    "wolverhampton wanderers": "Wolves", "blackburn rovers": "Blackburn",
    "derby county": "Derby", "west bromwich albion": "West Brom",
    "cardiff city": "Cardiff", "swansea city": "Swansea",
    "queens park rangers": "Queens Park Rangers", "millwall": "Millwall",
    "bolton wanderers": "Bolton Wanderers", "birmingham city": "Birmingham City",
    "lincoln city": "Lincoln City", "southampton fc": "Southampton",
    "fsv mainz 05": "Mainz", "atalanta bc": "Atalanta", "as roma": "Roma",
    "crystal palace": "Crystal Palace", "brighton and hove albion": "Brighton",
    "brighton": "Brighton", "aston villa": "Aston Villa",
    "newcastle united": "Newcastle", "leicester city": "Leicester",
    "nottingham forest": "Nottm Forest", "nottm forest": "Nottm Forest",
    "ipswich town": "Ipswich", "southampton": "Southampton",
    "real madrid": "Real Madrid", "barcelona": "Barcelona",
    "sevilla": "Sevilla", "valencia": "Valencia", "getafe": "Getafe",
    "osasuna": "Osasuna", "rayo vallecano": "Rayo Vallecano",
    "mallorca": "Mallorca", "las palmas": "Las Palmas", "alaves": "Alaves",
    "girona": "Girona", "leganes": "Leganes", "espanyol": "Espanyol",
    "valladolid": "Valladolid", "celta vigo": "Celta Vigo", "villarreal": "Villarreal",
    "real sociedad": "Real Sociedad", "lille": "Lille", "marseille": "Marseille",
    "monaco": "Monaco", "lyon": "Lyon", "lens": "Lens", "rennes": "Rennes",
    "nice": "Nice", "strasbourg": "Strasbourg", "nantes": "Nantes",
    "reims": "Reims", "montpellier": "Montpellier", "brest": "Brest",
    "toulouse": "Toulouse", "le havre": "Le Havre", "auxerre": "Auxerre",
    "angers": "Angers", "saint-etienne": "Saint-Etienne", "st. pauli": "St. Pauli",
    "holstein kiel": "Holstein Kiel", "heidenheim": "Heidenheim",
    "bochum": "Bochum", "union berlin": "Union Berlin",
    "werder bremen": "Werder Bremen", "mainz": "Mainz", "freiburg": "Freiburg",
    "wolfsburg": "Wolfsburg", "stuttgart": "Stuttgart", "hoffenheim": "Hoffenheim",
    "augsburg": "Augsburg", "bologna": "Bologna", "torino": "Torino",
    "monza": "Monza", "genoa": "Genoa", "verona": "Verona", "lecce": "Lecce",
    "udinese": "Udinese", "empoli": "Empoli", "cagliari": "Cagliari",
    "sassuolo": "Sassuolo", "frosinone": "Frosinone", "salernitana": "Salernitana",
    "roma": "Roma", "lazio": "Lazio", "fiorentina": "Fiorentina",
    "atalanta": "Atalanta", "napoli": "Napoli", "juventus": "Juventus",
    "inter": "Inter", "milan": "Milan", "liverpool": "Liverpool",
    "arsenal": "Arsenal", "chelsea": "Chelsea", "everton": "Everton",
    "fulham": "Fulham", "brentford": "Brentford", "wolves": "Wolves",
    "leicester": "Leicester", "bournemouth": "Bournemouth",
    "west ham": "West Ham", "newcastle": "Newcastle",
    "manchester city": "Manchester City", "manchester united": "Manchester United",
    "crystal palace": "Crystal Palace", "brighton": "Brighton",
    "aston villa": "Aston Villa", "tottenham": "Tottenham",
    "ipswich": "Ipswich", "nottm forest": "Nottm Forest",
}

def _match_team(api_name: str, league_name: str) -> Optional[str]:
    league_teams = ALL_LEAGUES.get(league_name, {})
    alower = api_name.lower().strip()
    if not alower:
        return None
    if alower in TEAM_MAP:
        mapped = TEAM_MAP[alower]
        if mapped in league_teams:
            return mapped
    for team in league_teams:
        tlower = team.lower()
        if tlower == alower or tlower in alower or alower in tlower:
            return team
    # Copertura mondiale: squadra fuori roster -> si usa il nome API cosi'
    # com'e'. expected_goals applica il profilo di lega di default e i rating
    # reali arriveranno coi risultati accumulati: la partita NON sparisce.
    return api_name

def _persist_skipped(skipped: List[dict]) -> None:
    """Salva le partite saltate (squadre non coperte) per renderle visibili
    nel report e nell'API invece di perderle in silenzio."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "saltate.json").write_text(json.dumps({
            "ts": datetime.utcnow().isoformat(), "saltate": skipped[:50],
            "n": len(skipped),
        }, indent=2))
    except Exception as e:
        logger.warning("persist saltate.json fallita: %s", e)


def get_skipped_matches() -> List[dict]:
    """Ultime partite saltate per squadre non coperte (da saltate.json).
    Ogni item riceve il timestamp dell'analisi per il filtro "ultime 24h"."""
    try:
        f = DATA_DIR / "saltate.json"
        if not f.exists():
            return []
        data = json.loads(f.read_text()) or {}
        ts = data.get("ts")
        items = data.get("saltate", [])
        for it in items:
            it["ts"] = ts
        return items
    except Exception:
        return []


def fetch_and_analyze_today():
    """Analizza il calendario delle prossime settimane (QUERY_WINDOW_DAYS,
    default 7gg) per le leghe di SPORTS_MAP dovute oggi.

    Gestione crediti piano free (500/mese): vengono interrogate SOLO le leghe
    la cui cache e' scaduta rispetto a SPORTS_INTERVAL_DAYS, in ordine di
    priorita' (intervallo minore = piu' importante) e con un tetto giornaliero
    DAILY_QUERY_BUDGET. Le leghe in eccedenza sono rinviate al giorno dopo
    (la finestra a 7gg copre: niente partite perse).

    Ritorna (total_matches, value_count, skipped): skipped e' la lista delle
    partite trovate ma NON analizzate perche' una o entrambe le squadre non
    sono nel roster di ALL_LEAGUES (con la copertura mondiale e' vuota).
    """
    if not os.getenv("ODDS_API_KEY"):
        logger.warning("ODDS_API_KEY mancante, skip calendario")
        return 0, 0, []
    clear_old_matches()
    today = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (datetime.utcnow() + timedelta(days=QUERY_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_matches = 0
    value_count = 0
    rejected_count = 0
    skipped: List[dict] = []
    # Leghe dovute oggi (cache scaduta), in ordine di priorita' (le core prima).
    due = sorted(((lg, key) for lg, key in SPORTS_MAP.items() if is_sport_due(key)),
                 key=lambda x: interval_for_sport(x[1]))
    if len(due) > DAILY_QUERY_BUDGET:
        rinviate = due[DAILY_QUERY_BUDGET:]
        logger.warning("Budget giornaliero %d: %d leghe rinviate a domani (%s)",
                       DAILY_QUERY_BUDGET, len(rinviate),
                       ", ".join(lg for lg, _ in rinviate[:8]))
        due = due[:DAILY_QUERY_BUDGET]
    for league, sport_key in due:
        try:
            raw = fetch_odds(sport=sport_key, commence_time_from=today, commence_time_to=window_end)
            for match in raw:
                mid = match.get("id", "")
                home_api = match.get("home_team", "")
                away_api = match.get("away_team", "")
                home_db = _match_team(home_api, league)
                away_db = _match_team(away_api, league)
                if not home_db or not away_db:
                    missing = [n for n, m in ((home_api, home_db), (away_api, away_db))
                               if not m]
                    skipped.append({
                        "league": league, "home": home_api, "away": away_api,
                        "non_coperte": missing,
                        "commence": match.get("commence_time", ""),
                    })
                    continue
                save_match(mid, league, home_db, away_db, match.get("commence_time", ""))
                total_matches += 1
                res = _analyze_match(mid, match, home_db, away_db, league)
                if res == "strong_value" or res == "value":
                    value_count += 1
                elif res == "rejected":
                    rejected_count += 1
        except Exception as e:
            logger.warning(f"Errore calendario {league}: {e}")
    if skipped:
        logger.warning("Partite saltate per squadre non coperte: %d (prime: %s)",
                       len(skipped),
                       [f"{s['home']} vs {s['away']} [{s['league']}]" for s in skipped[:5]])
        _persist_skipped(skipped)
    else:
        _persist_skipped([])
    logger.info(f"Calendario: {total_matches} partite | {value_count} value | "
                f"{rejected_count} filtrate | {len(skipped)} saltate")
    return total_matches, value_count, skipped

def _analyze_match(match_id, match, home_db, away_db, league):
    """Analizza un match: modello Poisson vs mercato (devig).

    Strategia (ricerca 2026): il valore esiste SOLO se il modello batte la
    probabilita' implicita del mercato (proxy della closing line), non se
    l'EV contro un singolo bookmaker e' positivo. Il flusso e':

    1. line shopping: per ogni esito si prende il MIGLIOR prezzo tra tutti
       i bookmaker (l'edge piu' facile da raccogliere);
    2. devig (metodo power): i prezzi di mercato vengono privati del margine
       -> probabilita' fair di mercato, con correzione del favourite-longshot
       bias;
    3. probabilita' finale = blend modello+mercato (riduce l'overconfidence
       del modello) + correzione longshot;
    4. EV calcolato sulla probabilita' blend, e il segnale e' valore solo se
       il modello batte il mercato di almeno MARKET_EDGE_MIN punti.
    """
    try:
        lam_h, lam_a = expected_goals(home_db, away_db)
    except Exception:
        return "error"
    p1, px, p2 = prob_1x2(lam_h, lam_a)

    home_api = (match.get("home_team") or "").strip().lower()
    away_api = (match.get("away_team") or "").strip().lower()

    # 1. Line shopping: miglior prezzo (e book) per esito, su tutti i bookmaker.
    h2h_prices: Dict[str, tuple] = {}    # "1"/"X"/"2" -> (prezzo, bookmaker)
    pinnacle_prices: Dict[str, float] = {}  # esito key -> prezzo Pinnacle (closing line sharp)
    for bm in match.get("bookmakers", []):
        bname = bm.get("title") or bm.get("key") or "Sconosciuto"
        is_pinnacle = ("pinnacle" in bname.lower())
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "h2h":
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip()
                    price = out.get("price")
                    if not name or not price or float(price) <= 1.0:
                        continue
                    low = name.lower()
                    if low == home_api:
                        esito = "1"
                    elif low == away_api:
                        esito = "2"
                    elif low in ("draw", "pareggio"):
                        esito = "X"
                    else:
                        continue
                    cur = h2h_prices.get(esito)
                    if cur is None or float(price) > cur[0]:
                        h2h_prices[esito] = (float(price), name, bname)
                    if is_pinnacle:
                        pinnacle_prices[esito] = float(price)

    # 1b. Asian Handicap (mercato 'spreads'): linee con entrambi i lati.
    #     home_line e' la linea vista dal lato casa (negativa = la casa dà gol).
    import re as _re
    spread_prices: Dict[float, Dict[str, tuple]] = {}
    for bm in match.get("bookmakers", []):
        bname = bm.get("title") or bm.get("key") or "Sconosciuto"
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            for out in mkt.get("outcomes", []):
                name = (out.get("name") or "").strip()
                price = out.get("price")
                point = out.get("point")
                if not name or point is None or not price or float(price) <= 1.0:
                    continue
                low = name.lower()
                low_clean = _re.sub(r"[+\-]?\d+(\.\d+)?$", "", low).strip()
                if low_clean == home_api or (home_api and home_api in low) or "home" in low_clean:
                    side = "home"
                elif (low_clean == away_api or (away_api and away_api in low)
                      or "away" in low_clean or "guest" in low_clean):
                    side = "away"
                else:
                    continue
                pt = float(point)
                home_line = pt if side == "home" else -pt
                if abs(home_line) < 0.001 or abs(home_line) > 3.5:
                    continue
                slot = spread_prices.setdefault(home_line, {})
                cur = slot.get(side)
                if cur is None or float(price) > cur[0]:
                    slot[side] = (float(price), name, bname)

    # 2. Probabilita' fair di mercato (devig power, corregge il longshot bias).
    market_h2h = market_implied({k: v[0] for k, v in h2h_prices.items()}) if len(h2h_prices) >= 2 else None

    # 3. Candidati: esito (chiave mercato) -> modello + mercato.
    # ML Ensemble: caricato una volta per l'analisi
    ensemble = _get_ensemble()

    candidates = []
    # OU2.5 escluso DEFINITIVAMENTE (06/09): il sistema elabora solo 1X2.
    # (Nessuna generazione di candidati Over/Under: leak sistematico.)
    for mkey, model_prob, market, prices in (
        ("1", p1, market_h2h, h2h_prices),
        ("X", px, market_h2h, h2h_prices),
        ("2", p2, market_h2h, h2h_prices),
    ):
        entry = prices.get(mkey)
        if not entry:
            continue
        price, display_esito, bookmaker = entry
        market_prob = market.get(mkey) if market else None
        final_prob = adjusted_probability(model_prob, market_prob, price,
                                          league=league)
        ev = compute_ev(final_prob, price)

        # ML Ensemble: se addestrato, calcola probabilità ensemble
        ensemble_prob = None
        if ensemble and ensemble.trained:
            ens_row = {
                "prob_1": p1, "prob_X": px, "prob_2": p2,
                "lam_h": lam_h, "lam_a": lam_a,
                "quota": price, "market_prob": market_prob,
                "market_edge": (model_prob - market_prob) if market_prob else None,
                "ev": ev, "prob": final_prob,
            }
            ens = ensemble.predict(ens_row)
            ensemble_prob = ens.get("ensemble_prob")
            # Se l'ensemble è confident, usa la sua probabilità per il blend
            if ens.get("confidence", 0) > 0.3:
                # Sostituisci il final_prob con l'ensemble (più calibrato)
                final_prob = ensemble_prob
                ev = compute_ev(final_prob, price)

        candidates.append({
            "score": ev,
            "ev": ev,
            "esito": display_esito,
            "quota": price,
            "bookmaker": bookmaker,
            "prob": final_prob,
            "prob_model": model_prob,
            "prob_ensemble": ensemble_prob,
            "market_prob": market_prob,
            "market_edge": (model_prob - market_prob) if market_prob is not None else None,
            "mercato": "1X2",
            "esito_key": mkey,
            # La lega DEVE viaggiare col candidato: `_candidate_status`
            # classifica ogni esito con `is_sane(..., league=cand["league"])`
            # e senza la chiave il gate STRATEGY_LEAGUES restava CIECO
            # (lega vuota = ammessa -> righe "value" in leghe vietate).
            "league": league,
        })

    # 3b. Candidati Asian Handicap: modello vs mercato devig per linea.
    # Serve solo al ledger previsioni (telemetria per mercato): il segnale
    # principale della schedina resta il miglior 1X2/Over-Under qui sotto.
    ah_candidates = []
    for home_line, sides in sorted(spread_prices.items()):
        if "home" not in sides or "away" not in sides:
            continue
        market_ah = market_implied({"home": sides["home"][0], "away": sides["away"][0]})
        for side, entry in (("home", sides["home"]), ("away", sides["away"])):
            price, disp, book = entry
            if side == "home":
                model_prob, _, _ = ah_outcome_probs(lam_h, lam_a, home_line, "home")
                market_prob = market_ah.get("home") if market_ah else None
                esito_disp = f"Home {home_line:+.2f}"
            else:
                away_line = -home_line
                model_prob, _, _ = ah_outcome_probs(lam_h, lam_a, away_line, "away")
                market_prob = market_ah.get("away") if market_ah else None
                esito_disp = f"Away {away_line:+.2f}"
            final_prob = adjusted_probability(model_prob, market_prob, price,
                                              league=league)
            ev = compute_ev(final_prob, price)
            ah_candidates.append({
                "score": ev,
                "ev": ev,
                "esito": esito_disp,
                "quota": price,
                "bookmaker": book,
                "prob": final_prob,
                "prob_model": model_prob,
                "market_prob": market_prob,
                "market_edge": (model_prob - market_prob) if market_prob is not None else None,
                "mercato": "AH",
                "league": league,
            })

    if not candidates:
        return "no_odds"
    # STRATEGIA SOLO FAVORITI (11/09): il candidato giocabile e' il miglior
    # EV tra i FAVORITI NETTI (quota <= ODDS_MAX e prob. di mercato massima).
    # Se nessun esito qualifica il match non genera segnali: gli esiti
    # sfavoriti/quote alte non finiscono ne' in schedina ne' nel ledger.
    shortlist = eligible_favourites(candidates)
    ah_shortlist = eligible_favourites(ah_candidates)
    favourites_ok = bool(shortlist)
    if favourites_ok:
        best = max(shortlist, key=lambda c: c["score"])
    else:
        # Solo per telemetria: l'esito col mercato piu' probabile, che sara'
        # registrato come rejected (nessuna puntata possibile).
        best = max(candidates, key=lambda c: (c.get("market_prob") or 0.0))

    # Snapshot prezzo per line movement tracking.
    try:
        for esito_key, entry in h2h_prices.items():
            record_snapshot(match_id, esito_key, entry[0],
                            bookmaker=entry[2],
                            market_prob=(market_h2h.get(esito_key)
                                         if market_h2h else None))
    except Exception as e:
        logger.debug(f"Snapshot prezzo fallito per {match_id}: {e}")

    # CLV: la prima volta che vediamo il match il prezzo e' quello del segnale;
    # le letture successive convergono verso la quota di chiusura del mercato.
    # La quota Pinnacle (se presente nel feed) e' la closing line sharp.
    signal_started = get_analysis_for_match(match_id) is None
    try:
        # Solo un candidato FAVORITO e' un segnale: registrare il CLV di un
        # esito scartato sporcherebbe le medie di chiusura.
        if favourites_ok:
            save_clv(match_id, str(best["esito"]), best["quota"],
                     signal_started=signal_started,
                     pinnacle_quota=pinnacle_prices.get(best.get("esito_key")))
    except Exception as e:
        logger.warning(f"Errore tracking CLV per {match_id}: {e}")

    # RLM / Steam detection: segnali di money sharp sul miglior esito.
    rlm_info = None
    steam_info = None
    try:
        rlm_info = detect_rlm(match_id, str(best["esito"]))
        steam_info = detect_steam(match_id, str(best["esito"]))
    except Exception:
        pass

    if favourites_ok:
        sane, reason = is_sane(best["prob"], best["quota"], best["ev"],
                               market_prob=best["market_prob"],
                               league=league)
    else:
        sane, reason = False, favourites_gate_reason()
    if not sane:
        status = "rejected"
        # Log solo a livello debug per non sporcare la dashboard
        logger.debug(f"FILTRATO: {home_db} vs {away_db} — {reason}")
    elif best["ev"] > 0.08 and (best["market_edge"] is None
                                 or best["market_edge"] >= MARKET_EDGE_STRONG):
        status = "strong_value"
    elif best["ev"] > 0.03:
        status = "value"
    else:
        status = "no_value"

    # RLM/steam come segnale di conferma: loggato per il report.
    # Non modifica lo status (ev e market_edge restano i criteri primari)
    # ma fornisce info aggiuntiva per valutare la qualita' del segnale.
    if rlm_info:
        logger.info(f"RLM su {match_id} {best['esito']}: "
                    f"{rlm_info['total_move_pct']:+.1f}% in "
                    f"{rlm_info['span_minutes']:.0f} min")
    if steam_info:
        logger.info(f"STEAM su {match_id} {best['esito']}: "
                    f"{steam_info['move_pct']:+.1f}% in "
                    f"{steam_info['span_minutes']:.0f} min")

    # p_over=None: colonna legacy prob_over del ledger (OU escluso, 06/09);
    # senza l'argomento posizionale la firma di tracker.save_analysis
    # slitta e ogni analisi crasha con TypeError (regressione 71b2c4c).
    save_analysis(match_id, lam_h, lam_a, p1, px, p2, None, best["ev"], best["esito"],
                  best["quota"], best["bookmaker"], status,
                  market_prob=best["market_prob"], market_edge=best["market_edge"])

    # Ledger previsioni: registra OGNI segnale proposto (1X2 e Asian
    # Handicap — OU2.5 escluso definitivamente il 06/09) col suo stato, per
    # la verifica a fine partita e la calibrazione del modello per mercato.
    try:
        # Solo favoriti netti (1X2 e AH) entrano nel ledger: gli esiti
        # scartati dal gate non vengono nemmeno registrati.
        for cand in shortlist + ah_shortlist:
            st = _candidate_status(cand)
            if st == "rejected":
                continue
            save_prediction(match_id, cand["mercato"], cand["esito"],
                            cand["quota"], cand["prob"], cand["ev"],
                            market_prob=cand.get("market_prob"),
                            market_edge=cand.get("market_edge"), status=st,
                            league=cand.get("league") or league)
    except Exception as e:
        logger.warning(f"Ledger previsioni per {match_id}: {e}")
    return status


def _candidate_status(cand: Dict) -> str:
    """Classifica un candidato: strong_value / value / no_value / rejected."""
    sane, _ = is_sane(cand["prob"], cand["quota"], cand["ev"],
                      market_prob=cand.get("market_prob"),
                      league=cand.get("league", ""))
    if not sane:
        return "rejected"
    if cand["ev"] > 0.08 and (cand["market_edge"] is None
                               or cand["market_edge"] >= MARKET_EDGE_STRONG):
        return "strong_value"
    if cand["ev"] > 0.03:
        return "value"
    return "no_value"

def get_calendar_formatted() -> str:
    rows = get_today_matches()
    if not rows:
        return "📅 *CALENDARIO DEL GIORNO*\n\nNessuna partita trovata oggi.\nAssicurati che ODDS_API_KEY sia configurata."
    msg = "📅 *CALENDARIO DEL GIORNO*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    current_league = ""
    for row in rows:
        mid, league, home, away, commence, status, _ = row
        if league != current_league:
            msg += f"🏆 *{league}*\n"
            current_league = league
        time_str = commence[11:16] if len(commence) > 16 else "--:--"
        ana = get_analysis_for_match(mid)
        if ana:
            (_, _, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito,
             best_quota, best_bookmaker, status, _, market_prob, market_edge) = ana
            ev_txt = f"+{best_ev*100:.1f}%" if best_ev > 0 else f"{best_ev*100:.1f}%"
            mkt_txt = f" | 🎯 mercato {market_edge*100:+.1f}pp" if market_edge is not None else ""
            if status == "strong_value":
                line = f"🔥 {time_str} {home} vs {away} → {best_esito} @ {best_quota:.2f} (EV {ev_txt}{mkt_txt})"
            elif status == "value":
                line = f"🟡 {time_str} {home} vs {away} → {best_esito} @ {best_quota:.2f} (EV {ev_txt}{mkt_txt})"
            elif status == "rejected":
                line = f"❌ {time_str} {home} vs {away} → FILTRATO"
            else:
                line = f"⚪ {time_str} {home} vs {away}"
        else:
            line = f"⚪ {time_str} {home} vs {away} (analisi in corso)"
        msg += line + "\n"
    msg += "\n💡 Usa `/analisi` per aggiornare quote e analisi."
    return msg

def get_value_picks_for_schedina() -> List[Dict]:
    conn = None
    try:
        from tracker import _get_conn
        conn = _get_conn()
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute('''SELECT m.league, m.home_team, m.away_team, a.best_esito, a.best_quota, a.best_bookmaker, a.best_ev, a.lam_h, a.lam_a, a.market_edge, a.market_prob
                     FROM matches m JOIN match_analysis a ON m.id = a.match_id
                     WHERE m.commence_time LIKE ? AND a.status IN ('value','strong_value')
                     ORDER BY a.best_ev DESC LIMIT 7''', (f"{today}%",))
        rows = c.fetchall()
        picks = []
        for r in rows:
            picks.append({
                "league": r[0], "home": r[1], "away": r[2], "esito": r[3],
                "quota": r[4], "bookmaker": r[5], "ev": r[6], "lam_h": r[7], "lam_a": r[8],
                "market_edge": r[9], "market_prob": r[10],
                "evento": f"{r[0]} – {r[1]} vs {r[2]}"
            })
        return picks
    except Exception as e:
        logger.warning(f"Errore get_value_picks: {e}")
        return []
    finally:
        if conn: conn.close()

def format_schedina(picks: List[Dict], bankroll: float = 100.0) -> str:
    if not picks:
        return "📋 *SCHEDINA DEL GIORNO*\n\nNessuna partita con valore positivo trovata oggi.\nRiprova più tardi con `/analisi`."
    msg = "📋 *SCHEDINA DEL GIORNO*\n"
    msg += f"🗓 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += "🎯 *MIGLIORI SINGOLE DEL GIORNO*\n"
    msg += "⚠️ Gioca SEMPRE le singole. La multipla distrugge il valore.\n\n"
    total_stake = 0.0
    for i, p in enumerate(picks, 1):
        prob = p["ev"] + (1/p["quota"])
        pro = get_pro_stake(bankroll, prob, p["quota"])
        stake = pro["stake"]
        total_stake += stake
        mkt_txt = f" | 🎯 batte il mercato di {p['market_edge']*100:+.1f}pp" if p.get("market_edge") is not None else ""
        msg += (
            f"*{i}. {p['evento']}*\n"
            f"   🎯 {p['esito']} @ {p['quota']:.2f} ({p['bookmaker']})\n"
            f"   📈 EV: +{p['ev']*100:.1f}%{mkt_txt} | Stake: €{stake:.2f} ({pro['stake_pct_of_bankroll']:.1f}% bankroll)\n"
            f"   🛡 Filtri: Kelly 1/4 | Cap 1-2% | EV {EV_MIN*100:g}-{EV_MAX*100:g}% | "
            f"Odds {ODDS_MIN:.2f}-{ODDS_MAX:.2f} | Edge ≥ +{MARKET_EDGE_MIN*100:.0f}pp | "
            f"Mercato: devig power\n\n"
        )
    msg += f"💵 *Investimento totale:* €{total_stake:.2f} ({(total_stake/bankroll*100):.1f}% bankroll)\n"
    msg += f"💰 *Bankroll di riferimento:* €{bankroll:.2f}\n\n"
    # --- Multipla prolungata (massimo 7 esiti) ---
    msg += "\n" + build_multipla_block(picks, bankroll)
    return msg


def build_multipla(picks: List[Dict], max_legs: int = 7) -> Optional[Dict]:
    """Costruisce una multipla dai migliori esiti con quota e prob combinate.

    La prob combinata usa il denominatore corretto (prob = ev + 1/quota),
    stimata dall'EV di ciascun esito. Ritorna None se ci sono meno di 2 esiti.
    """
    if len(picks) < 2:
        return None
    legs = picks[:max_legs]
    odds = [p["quota"] for p in legs]
    probs = [p["ev"] + (1.0 / p["quota"]) for p in legs]
    total_quota = combined_quota(odds)
    total_prob = combined_probability(probs)
    ev = compute_ev(total_prob, total_quota)
    return {
        "legs": legs,
        "quota": total_quota,
        "prob": total_prob,
        "ev": ev,
        "esiti": " + ".join(p["esito"] for p in legs),
    }


def build_multipla_block(picks: List[Dict], bankroll: float = 100.0) -> str:
    """Formatta la sezione multipla prolungata con risk management automatico."""
    mp = build_multipla(picks)
    if not mp:
        return ""
    stake = multipla_stake(bankroll, mp["prob"], mp["quota"])
    ev_txt = f"+{mp['ev']*100:.1f}%" if mp['ev'] > 0 else f"{mp['ev']*100:.1f}%"
    if mp["ev"] >= 0.05:
        verdict = "🟢 MULTIPLA ACCETTABILE (EV buono)"
    elif mp["ev"] >= 0:
        verdict = "🟡 MULTIPLA MARGINALE (EV ~0)"
    else:
        verdict = "🔴 MULTIPLA NEGATIVA — sconsigliata"
    block = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *MULTIPLA PROLUNGATA* (massimo 7 esiti)\n"
        "⚠️ La multipla aumenta il rischio: vince solo se passano TUTTI gli esiti.\n\n"
    )
    for i, p in enumerate(mp["legs"], 1):
        block += f"{i}. {p['esito']} @ {p['quota']:.2f}\n"
    block += (
        "\n"
        f"💯 Quota combinata: @{mp['quota']:.2f}\n"
        f"📈 Probabilità congiunta: {mp['prob']*100:.1f}% | EV: {ev_txt}\n"
        f"💰 Stake suggerito (1/8 Kelly, cap 1%): *€{stake:.2f}*\n\n"
        f"🛡 {verdict}\n"
        f"✅ Giocare solo se la combinazione resta sotto l'1% del bankroll."
    )
    return block
