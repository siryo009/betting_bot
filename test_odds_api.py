"""Test SPORTS_MAP: le coppe devono avere la chiave ufficiale the-odds-api
e i dati squadre in ALL_LEAGUES (senza, il matching squadre salta)."""

import pytest

from odds_api import SPORTS_MAP
from leagues_data import ALL_LEAGUES

# Chiavi ufficiali the-odds-api (documentazione the-odds-api.com/sports-apis)
OFFICIAL_CUP_KEYS = {
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    "FA Cup": "soccer_fa_cup",
    "EFL Cup": "soccer_england_efl_cup",
    "Copa Libertadores": "soccer_conmebol_copa_libertadores",
    "EFL Championship": "soccer_efl_champ",
    "Swiss Super League": "soccer_switzerland_superleague",
}


def test_coppe_presenti_in_sports_map():
    for cup in OFFICIAL_CUP_KEYS:
        assert cup in SPORTS_MAP, f"{cup} manca da SPORTS_MAP"


def test_coppe_con_chiave_ufficiale():
    for cup, key in OFFICIAL_CUP_KEYS.items():
        assert SPORTS_MAP[cup] == key, f"{cup}: chiave {SPORTS_MAP[cup]} != ufficiale {key}"


def test_copertura_mondiale_completa():
    """SPORTS_MAP copre TUTTE le competizioni di calcio the-odds-api
    (66 chiavi soccer_, verificate su the-odds-api.com/sports-apis)."""
    assert len(SPORTS_MAP) >= 60
    for league, key in SPORTS_MAP.items():
        assert key.startswith("soccer_"), f"{league}: chiave non soccer: {key}"


def test_lega_con_roster_ha_dati():
    """Le leghe con roster in ALL_LEAGUES devono averlo non vuoto."""
    for league in ALL_LEAGUES:
        if league in SPORTS_MAP:
            assert ALL_LEAGUES[league], f"{league} con roster vuoto"


def test_rotazione_crediti():
    """Ogni lega ha un intervallo esplicito e il costo mensile sta nel
    piano free the-odds-api (500 crediti/mese)."""
    from odds_api import interval_for_sport, SPORTS_INTERVAL_DAYS
    # Profilo 25/09/2026: le 20 leghe AMMESSE stanno a 2gg — l'API pubblica le
    # odds con 1-3 giorni di anticipo, quindi con rotazione a 7gg una lega
    # interrogata il giorno X non vedeva MAI le partite del weekend X+4
    # (misurato: a 7 giorni Serie A/Bundesliga/La Liga/Eredivisie a 0 eventi,
    # mentre MLS 15 e Liga MX 9 nella stessa finestra), e alla successiva
    # interrogazione (X+7) erano passate -> zero candidati per sempre.
    # Il resto resta dormiente (es. Copa America a 30gg).
    assert interval_for_sport("soccer_epl") == 2
    assert interval_for_sport("soccer_turkey_super_league") == 2
    assert interval_for_sport("soccer_conmebol_copa_america") == 30
    # ⚠️ 29/09/2026 — LE NAZIONALI SONO PASSATE A CORE. Fino al 28/09 questa
    # riga asseriva `== 30` (dormiente) ed era corretta: UEFA Nations League
    # NON era una lega ammessa. Da quando il gate la ammette, tenerla a 30gg
    # sarebbe il difetto del 24/09 (una lega giocabile mai interrogata = zero
    # candidati qualunque soglia). Ora segue gli intervalli delle ammesse.
    assert interval_for_sport("soccer_uefa_nations_league") == 2
    assert interval_for_sport("soccer_africa_cup_of_nations") == 2
    # ogni lega in SPORTS_MAP deve avere un intervallo ESPLICITO
    # (niente default silenziosi: prima "Chile Primera" finiva a 1 = 30/mese)
    for league, key in SPORTS_MAP.items():
        assert league in SPORTS_INTERVAL_DAYS, f"{league} senza intervallo"
        assert interval_for_sport(key) == SPORTS_INTERVAL_DAYS[league]


def test_leghe_ammesse_mai_dormienti():
    """Una lega GIOCABILE non puo' stare a 30gg di rotazione.

    Difetto misurato il 24/09/2026: 10 leghe ammesse (Turkey Super Lig,
    Allsvenskan, Argentina Primera, Austrian Bundesliga, Eliteserien, J1
    League, K League 1, Scottish Premiership, Superliga Danimarca, Swiss
    Super League) erano a 30gg, cioe' DORMIENTI di fatto: con partite in
    calendario non venivano mai interrogate, quindi non potevano produrre
    alcun candidato qualunque fosse la soglia di edge/EV. Dal 11/09 nelle
    20 leghe ammesse c'e' una sola prediction su 688 righe di ledger.

    Secondo difetto misurato il 25/09/2026: a 7gg la rotazione NON cattura le
    odds, perche' l'API le pubblica con 1-3 giorni di anticipo. Le ammesse
    stanno quindi a **2 giorni** — che e' anche il massimo sostenibile dal
    tetto crediti (costo mensile teorico 371/460; a 1gg sarebbe 671, vedi
    `test_budget_mensile_piano_free`). L'uguaglianza a 2 e' voluta: un
    allargamento (o un restringimento a 1gg che sfonda il tetto) deve
    rompere il test, non passare in silenzio.
    """
    from odds_api import SPORTS_INTERVAL_DAYS
    import value_filter as vf
    for lg in list(vf.STRATEGY_LEAGUES) + sorted(vf.PROBATION_LEAGUES):
        assert lg in SPORTS_INTERVAL_DAYS, f"{lg} senza intervallo"
        assert SPORTS_INTERVAL_DAYS[lg] == 2, (
            f"{lg} e' a {SPORTS_INTERVAL_DAYS[lg]}gg: le leghe ammesse devono "
            "stare a 2 giorni (le odds nascono 1-3 giorni prima del kickoff; "
            "il tetto crediti non sostiene 1gg)")


def test_budget_mensile_piano_free():
    """Costo mensile totale della rotazione <= 460 crediti (500 del piano
    free, con margine per /scores e trigger manuali)."""
    from odds_api import interval_for_sport
    cost = sum(30.0 / interval_for_sport(key) for key in SPORTS_MAP.values())
    assert cost <= 460, f"costo mensile {cost:.0f} oltre il budget free"


def test_roster_coppe_coprono_le_top():
    """Le coppe nazionali copiano i roster dei campionati: almeno le squadre
    principali devono essere riconosciute (es. Inter in Coppa Italia)."""
    assert "Inter" in ALL_LEAGUES["Coppa Italia"]
    # regressione: le squadre di Serie B giocano la Coppa Italia
    # (Parma-Cremonese 1/9/2026 veniva persa: roster solo Serie A)
    assert "Parma" in ALL_LEAGUES["Coppa Italia"]
    assert "Cremonese" in ALL_LEAGUES["Coppa Italia"]
    assert "Real Madrid" in ALL_LEAGUES["Copa del Rey"]
    assert "Paris Saint-Germain" in ALL_LEAGUES["Coupe de France"]
    assert "Bayern Munich" in ALL_LEAGUES["DFB Pokal"]
    assert "Inter" in ALL_LEAGUES["Champions League"]
    # coppe internazionali: roster = merge dei campionati d'origine
    assert "Manchester City" in ALL_LEAGUES["FA Cup"]
    assert "Leeds United" in ALL_LEAGUES["EFL Cup"]
    assert "Flamengo" in ALL_LEAGUES["Copa Libertadores"]
    assert "River Plate" in ALL_LEAGUES["Copa Libertadores"]
    # regressione: West Ham e Wolves retrocesse giocano in Championship
    # (West Ham vs Wolves 1/9/2026 veniva persa: lega non interrogata)
    assert "West Ham" in ALL_LEAGUES["EFL Championship"]
    assert "Wolves" in ALL_LEAGUES["EFL Championship"]
    # regressione: Super League svizzera (Zurigo vs Young Boys 1/9/2026)
    assert "Young Boys" in ALL_LEAGUES["Swiss Super League"]
    assert "FC Zurich" in ALL_LEAGUES["Swiss Super League"]


def test_match_team_fallback_nome_api():
    """Squadra fuori roster -> si usa il nome API (la partita non sparisce)."""
    from fixture_engine import _match_team
    assert _match_team("Galatasaray", "Turkey Super Lig") == "Galatasaray"
    assert _match_team("Sconosciuta FC", "Serie A") == "Sconosciuta FC"


def test_expected_goals_con_squadre_sconosciute():
    """expected_goals non alza piu' errori: profilo di lega di default."""
    from poisson_engine import expected_goals
    lam_h, lam_a = expected_goals("Sconosciuta FC", "Altra FC")
    assert lam_h > 0 and lam_a > 0
    # mischiata con una squadra conosciuta funziona comunque
    lam_h2, lam_a2 = expected_goals("Inter", "Sconosciuta FC")
    assert lam_h2 > 0 and lam_a2 > 0


def test_match_team_copre_nuove_leghe():
    """Il matching riconosce le squadre dei campionati appena aggiunti
    (West Ham/Wolves in Championship, Young Boys/Zurigo in Svizzera)."""
    from fixture_engine import _match_team
    assert _match_team("West Ham", "EFL Championship") == "West Ham"
    assert _match_team("Wolves", "EFL Championship") == "Wolves"
    # l'API the-odds-api usa i nomi completi: alias obbligatori
    assert _match_team("West Ham United", "EFL Championship") == "West Ham"
    assert _match_team("Wolverhampton Wanderers", "EFL Championship") == "Wolves"
    assert _match_team("Blackburn Rovers", "EFL Championship") == "Blackburn"
    assert _match_team("Southampton", "EFL Championship") == "Southampton"
    assert _match_team("Bolton Wanderers", "EFL Championship") == "Bolton Wanderers"
    assert _match_team("Lincoln City", "EFL Championship") == "Lincoln City"
    assert _match_team("Birmingham City", "EFL Championship") == "Birmingham City"
    assert _match_team("Young Boys", "Swiss Super League") == "Young Boys"
    assert _match_team("FC Zurich", "Swiss Super League") == "FC Zurich"
    # l'API puo' usare l'umlaut o la forma corta: entrambe devono matchare
    assert _match_team("Zürich", "Swiss Super League") == "Zürich"
    assert _match_team("Zurich", "Swiss Super League") == "FC Zurich"


def test_fetch_analizza_anche_squadre_sconosciute(monkeypatch, tmp_path):
    """Con la copertura mondiale NESSUNA partita viene piu' saltata:
    anche le squadre fuori roster vengono analizzate (profilo di default)."""
    import tracker
    import fixture_engine
    import odds_api
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t.db")
    tracker.init_db()
    monkeypatch.setattr(fixture_engine, "DATA_DIR", tmp_path)
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)  # cache vuota -> tutte dovute
    # Budget alto: con cache vuota TUTTE le leghe sono "dovute" e il tetto
    # giornaliero (default 12) taglierebbe il gruppo delle ammesse a 2gg
    # lasciando fuori la lega che questo test vuole esercitare. Qui il tetto
    # non e' l'oggetto del test (lo verifica `test_budget_giornaliero_cap`).
    monkeypatch.setattr(fixture_engine, "DAILY_QUERY_BUDGET", 80)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    payload = [
        {"id": "m1", "home_team": "Inter", "away_team": "Napoli",
         "commence_time": "2026-09-01T18:00:00Z", "bookmakers": [
             {"title": "Pinnacle", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Inter", "price": 1.90},
                 {"name": "Napoli", "price": 4.20},
                 {"name": "Draw", "price": 3.40},
             ]}]},
         ]},
        {"id": "m2", "home_team": "Sconosciuta FC", "away_team": "Altra FC",
         "commence_time": "2026-09-01T19:00:00Z", "bookmakers": []},
    ]

    def fake_fetch(sport=None, **kw):
        return payload if sport == "soccer_italy_serie_a" else []
    monkeypatch.setattr(fixture_engine, "fetch_odds", fake_fetch)

    total, value, skipped = fixture_engine.fetch_and_analyze_today()
    assert total == 2          # ENTRAMBE analizzate (anche le sconosciute)
    assert skipped == []       # niente partite perse in silenzio


def test_budget_giornaliero_cap(monkeypatch, tmp_path):
    """Il tetto giornaliero limita le chiamate API: con budget 1 viene
    interrogata SOLO la lega piu' prioritaria (intervallo minore = le 20
    leghe ammesse a 2gg dal 25/09; prima del cambio il gruppo a 3gg)."""
    import tracker
    import fixture_engine
    import odds_api
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t.db")
    tracker.init_db()
    monkeypatch.setattr(fixture_engine, "DATA_DIR", tmp_path)
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fixture_engine, "DAILY_QUERY_BUDGET", 1)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    calls = []

    def fake_fetch(sport=None, **kw):
        calls.append(sport)
        return []
    monkeypatch.setattr(fixture_engine, "fetch_odds", fake_fetch)

    fixture_engine.fetch_and_analyze_today()
    assert len(calls) == 1
    # La lega scelta deve appartenere al gruppo a intervallo MINIMO (le piu'
    # importanti): fissare il nome legherebbe il test all'ordine interno di
    # SPORTS_MAP, che non e' un contratto.
    minimo = min(odds_api.interval_for_sport(k) for k in SPORTS_MAP.values())
    assert odds_api.interval_for_sport(calls[0]) == minimo


# ---------------------------------------------------------------------------
# Stagger rotazione (10/09): le leghe core a 3gg non devono sincronizzarsi
# tutte nello stesso giorno, altrimenti ci sono 2 giorni su 3 senza analisi.
# ---------------------------------------------------------------------------


def _write_cache(tmp_path, sport_key, ts):
    (tmp_path / f"toa_{sport_key}.json").write_text(
        __import__("json").dumps({"ts": ts, "payload": [], "remaining": 500}))


def test_stagger_spalma_le_leghe_core():
    """Le 20 leghe AMMESSE (intervallo 2 dal 25/09) hanno fasi diverse: non
    scadono tutte lo stesso giorno, altrimenti ci sarebbero giorni di
    calendario senza nessuna lega giocabile interrogata.

    Con intervallo 2 le fasi possibili sono {0, 1}: il test pretende che
    ENTRAMBE siano rappresentate (se tutte le leghe cadessero nello stesso
    giorno, meta' dei giorni sarebbe a zero analisi).
    """
    import odds_api
    import value_filter as vf
    ammesse = [SPORTS_MAP[lg]
               for lg in list(vf.STRATEGY_LEAGUES) + sorted(vf.PROBATION_LEAGUES)]
    assert len(ammesse) >= 15
    fasi = {odds_api._rotation_phase(k, 2) for k in ammesse}
    assert fasi == {0, 1}, f"leghe ammesse sincronizzate: fasi {fasi}"


def test_stagger_scadenza_sul_giorno_di_fase(monkeypatch, tmp_path):
    """Con cache vecchia di 1 giorno, una lega core e' dovuta SOLO sul suo
    giorno di fase (e non sui giorni vicini)."""
    import time as _t
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0  # giorno fisso: day = now // 86400
    monkeypatch.setattr(odds_api.time, "time", lambda: now)
    day = int(now // 86400)

    key = "soccer_italy_serie_a"
    interval = odds_api.interval_for_sport(key)
    phase = odds_api._rotation_phase(key, interval)
    # cache vecchia di 1 giorno (eta' < ttl di 3 giorni)
    _write_cache(tmp_path, key, now - 86400)
    assert odds_api.is_sport_due(key) == (day % interval == phase)

    # cache vecchia di 2 giorni: ancora dentro il ttl, stessa regola di fase
    _write_cache(tmp_path, key, now - 2 * 86400)
    assert odds_api.is_sport_due(key) == (day % interval == phase)


def test_stagger_non_anticipa_le_leghe_30gg(monkeypatch, tmp_path):
    """Le leghe a 30gg (dormienti) NON vengono anticipate dal giorno di
    fase: restano dovute solo a scadenza intervallo (zero costi extra).

    Esempio: una competizione di nazionali fuori strategia. NB le leghe
    AMMESSE stanno a 2gg (dal 25/09) e dal 29/09 anche UEFA Nations League e
    Africa Cup of Nations sono in CORE: per una lega dormiente va scelta una
    competizione che NON e' ammessa (qui Copa America).
    """
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(odds_api.time, "time", lambda: now)

    key = "soccer_conmebol_copa_america"
    assert odds_api.interval_for_sport(key) == 30
    _write_cache(tmp_path, key, now - 2 * 86400)  # eta' 2 giorni
    assert odds_api.is_sport_due(key) is False
    # anche sul giorno di fase (se cadesse oggi) non scatta: niente anticipo
    phase = odds_api._rotation_phase(key, 30)
    day = int(now // 86400)
    if day % 30 == phase:
        _write_cache(tmp_path, key, now - 86400)
        assert odds_api.is_sport_due(key) is False


def test_stagger_scadenza_per_intervallo_invariata(monkeypatch, tmp_path):
    """A scadenza intervallo la lega e' dovuta comunque, qualunque sia la
    fase (la regola di stagger NON allunga mai l'intervallo)."""
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(odds_api.time, "time", lambda: now)

    key = "soccer_italy_serie_a"
    interval = odds_api.interval_for_sport(key)
    _write_cache(tmp_path, key, now - interval * 86400 - 1)
    assert odds_api.is_sport_due(key) is True


def test_scores_cache_persiste_i_crediti(monkeypatch, tmp_path):
    """Bug 12/09: `fetch_scores` non salvava `remaining` nella cache dei
    punteggi, quindi `get_remaining()`/`get_quota()` (guardia proattiva +
    credit watchdog) vedevano solo il consumo della rotazione quote: il
    contatore restava fermo a 58 mentre l'API ne riportava 6 -> nessun
    throttle, nessun alert, crediti bruciati dal settlement fino a zero.
    La cache dei punteggi deve rendere visibile il credito residuo
    restituito dall'header `x-requests-remaining`."""
    import json
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        headers = {"x-requests-remaining": "6"}

        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": "m1", "home_team": "A", "away_team": "B",
                     "completed": True,
                     "scores": [{"name": "A", "score": "1"}]}]

    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _Resp())
    odds_api.fetch_scores("soccer_italy_serie_a")

    cached = json.loads(
        (tmp_path / "toa_scores_soccer_italy_serie_a.json").read_text())
    assert cached["remaining"] == 6
    assert odds_api.get_remaining() == 6
    assert odds_api.get_quota() == (6, 1)


def test_get_remaining_usa_la_lettura_piu_recente(monkeypatch, tmp_path):
    """Il contatore non deve restare inchiodato a un valore STANTIO.

    Caso reale del 12/09: chiave nuova con 452 crediti, ma le cache quote
    scritte con la chiave vecchia portavano ancora `remaining: 58` -> con il
    MINIMO tra le cache la lettura restava 58 per settimane (le cache quote
    si rinnovano ogni 3-30 giorni) e la rotazione veniva throttled a vuoto.
    Vale la lettura piu' recente; con `ts` preservato in `fetch_scores` e'
    `remaining_ts` a datare il valore del credito.
    """
    import json
    import time
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = time.time()
    # cache QUOTE vecchia (chiave precedente): 58 crediti, letta 2 giorni fa
    (tmp_path / "toa_soccer_italy_serie_a.json").write_text(json.dumps(
        {"ts": now - 2 * 86400, "payload": [], "remaining": 58,
         "remaining_ts": now - 2 * 86400}))
    # cache PUNTEGGI fresca (chiave nuova): 452 crediti, letta ora;
    # attenzione: `ts` puo' essere quello vecchio, `remaining_ts` e' ora
    (tmp_path / "toa_scores_soccer_italy_serie_a.json").write_text(json.dumps(
        {"ts": now - 86400, "payload": [], "remaining": 452,
         "remaining_ts": now}))
    assert odds_api.get_remaining() == 452
    # anche get_quota (usato da /api/health) deve riportare la lettura fresca
    assert odds_api.get_quota() == (452, 2)


def _credit_cache(tmp_path, name, remaining, ts, remaining_ts=None):
    """Telemetria crediti di una lega: quante richieste restavano e QUANDO
    e' stata letta quella risposta dell'API."""
    import json
    (tmp_path / f"toa_{name}.json").write_text(json.dumps(
        {"ts": ts, "payload": [], "remaining": remaining,
         "remaining_ts": ts if remaining_ts is None else remaining_ts}))


def _credit_env(monkeypatch, tmp_path):
    """Isola la telemetria crediti e ritorna il modulo odds_api."""
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    return odds_api


def test_burn_rate_misura_il_consumo_recente(monkeypatch, tmp_path):
    """Il RITMO di consumo, non il livello, e' il rischio a meta' mese.

    329 -> 273 crediti in 24h = ~56/giorno: e' il numero che il 15/09 ha
    rivelato che il budget non arrivava al reset, mentre la media a 340h
    diceva 15/giorno (includeva giorni di settlement in pausa e il cambio
    di chiave). La finestra del misuratore e' corta di proposito.
    """
    import time
    oa = _credit_env(monkeypatch, tmp_path)
    now = time.time()
    _credit_cache(tmp_path, "soccer_epl", 329, now - 86400)
    _credit_cache(tmp_path, "soccer_italy_serie_a", 273, now)
    b = oa.credit_burn_rate()
    assert b["rate_per_day"] == pytest.approx(56.0, abs=0.5)
    assert b["window_hours"] == pytest.approx(24.0, abs=0.1)
    assert b["samples"] == 2 and b["remaining"] == 273


def test_burn_rate_fallback_dichiara_la_finestra_vera(monkeypatch, tmp_path):
    """Una sola lettura DENTRO la finestra: si allarga alla storia
    disponibile, ma `window_hours` dice la finestra VERA (mai spacciare
    240h per 48h, altrimenti il ritmo sembra 5 volte piu' alto)."""
    import time
    oa = _credit_env(monkeypatch, tmp_path)
    now = time.time()
    _credit_cache(tmp_path, "soccer_epl", 500, now - 10 * 86400)
    _credit_cache(tmp_path, "soccer_italy_serie_a", 300, now)
    b = oa.credit_burn_rate()
    assert b["window_hours"] == pytest.approx(240.0, abs=1.0)
    assert b["rate_per_day"] == pytest.approx(20.0, abs=0.5)


def test_burn_rate_none_se_i_crediti_risalgono(monkeypatch, tmp_path):
    """Chiave cambiata o reset del piano: il delta positivo non e' consumo.
    Meglio None di un ritmo negativo (che farebbe proiezioni assurde)."""
    import time
    oa = _credit_env(monkeypatch, tmp_path)
    now = time.time()
    _credit_cache(tmp_path, "soccer_epl", 58, now - 86400)
    _credit_cache(tmp_path, "soccer_italy_serie_a", 500, now)
    assert oa.credit_burn_rate() is None


def test_burn_rate_none_con_una_sola_lettura(monkeypatch, tmp_path):
    import time
    oa = _credit_env(monkeypatch, tmp_path)
    _credit_cache(tmp_path, "soccer_epl", 300, time.time())
    assert oa.credit_burn_rate() is None


def test_budget_alert_quando_il_ritmo_non_arriva_al_reset(monkeypatch,
                                                          tmp_path):
    """273 crediti a 58/giorno finiscono il 19/09, con il reset il 01/10:
    il residuo sembra alto ma il budget NON arriva -> alert (e' il campanello
    che le soglie fisse 50/20/10/5 non danno, perche' tacciono sopra 50).

    `now` e' FISSO: il test non deve scadere col calendario."""
    import time
    from datetime import datetime, timezone
    oa = _credit_env(monkeypatch, tmp_path)
    now = time.time()
    _credit_cache(tmp_path, "soccer_epl", 331, now - 86400)
    _credit_cache(tmp_path, "soccer_italy_serie_a", 273, now)
    st = oa.credit_budget_status(
        now=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    assert st["remaining"] == 273
    assert st["rate_per_day"] == pytest.approx(58.0, abs=0.5)
    assert st["days_to_reset"] == 15
    assert st["days_left"] == pytest.approx(4.7, abs=0.1)
    assert st["exhaustion_date"] == "2026-09-20"   # 273 / 58 = ~4.7 giorni
    assert st["alert"] is True


def test_budget_senza_alert_se_il_ritmo_regge(monkeypatch, tmp_path):
    """Ritmo 5/giorno con lo stesso residuo: il budget arriva al reset."""
    import time
    from datetime import datetime, timezone
    oa = _credit_env(monkeypatch, tmp_path)
    now = time.time()
    _credit_cache(tmp_path, "soccer_epl", 278, now - 86400)
    _credit_cache(tmp_path, "soccer_italy_serie_a", 273, now)
    st = oa.credit_budget_status(
        now=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    assert st["rate_per_day"] == pytest.approx(5.0, abs=0.5)
    assert st["alert"] is False


def test_budget_senza_consumo_misurabile_non_allerta(monkeypatch, tmp_path):
    """Nessuna telemetria (o una sola lettura): nessuna proiezione, nessun
    alert inventato — resta solo il residuo."""
    import time
    oa = _credit_env(monkeypatch, tmp_path)
    _credit_cache(tmp_path, "soccer_epl", 300, time.time())
    st = oa.credit_budget_status()
    assert st["remaining"] == 300 and st["rate_per_day"] is None
    assert st["alert"] is False and st["exhaustion_date"] is None


def test_get_remaining_senza_remaining_ts_usa_ts(monkeypatch, tmp_path):
    """Ripiego: cache di formato vecchio (senza `remaining_ts`) ordinate
    per `ts`, per non perdere la telemetria dei crediti."""
    import json
    import time
    import odds_api
    monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
    now = time.time()
    (tmp_path / "toa_a.json").write_text(json.dumps(
        {"ts": now - 3600, "payload": [], "remaining": 10}))
    (tmp_path / "toa_b.json").write_text(json.dumps(
        {"ts": now, "payload": [], "remaining": 33}))
    assert odds_api.get_remaining() == 33


# ---------------------------------------------------------------------------
# HARD STOP crediti (21/09/2026): sotto 5 crediti NESSUNA chiamata HTTP
# ---------------------------------------------------------------------------

def _no_http(*a, **k):
    raise AssertionError("nessuna chiamata HTTP verso the-odds-api")


def test_credits_hard_stopped_sotto_soglia(monkeypatch, tmp_path):
    """Sotto 5 crediti il blocco e' TOTALE; a 5 e a telemetria assente no.

    La soglia e' `ODDS_CREDIT_HARD_STOP` (default 5): "sotto i 5" = 4 ->
    blocco. Fail-open senza telemetria: non sapendo quanto resta non si
    ferma tutto (stessa direzione di `should_query_sport`).
    """
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    # nessuna cache: telemetria assente -> non si blocca
    assert odds_api.credits_hard_stopped() is False
    # 5 crediti: ancora sopra la soglia (< 5) -> consentito
    _credit_cache(tmp_path, "soccer_epl", 5, time.time())
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    assert odds_api.credits_hard_stopped() is False
    # 4 crediti: blocco totale
    _credit_cache(tmp_path, "soccer_epl", 4, time.time() + 1)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    assert odds_api.credits_hard_stopped() is True


def test_credits_hard_stop_segue_la_costante(monkeypatch, tmp_path):
    """La soglia e' configurabile (`ODDS_CREDIT_HARD_STOP`): con 10 crediti
    residui e soglia 20 il blocco scatta comunque."""
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "CREDIT_HARD_STOP", 20)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    _credit_cache(tmp_path, "soccer_epl", 10, time.time())
    assert odds_api.credits_hard_stopped() is True


def test_telemetria_vecchia_sotto_soglia_non_blocca(monkeypatch, tmp_path):
    """Una telemetria sotto soglia ma VECCHIA non blocca: la cache si aggiorna
    solo con una chiamata e le chiamate sono bloccate, quindi senza il probe
    una chiave sostituita o il reset mensile resterebbero invisibili per
    sempre (blocco eterno).
    """
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setattr(odds_api, "_credit_probe_logged", False)
    _credit_cache(
        tmp_path, "soccer_epl", 1,
        time.time() - odds_api.CREDIT_HARD_STOP_MAX_AGE_H * 3600 - 60)
    assert odds_api.credits_hard_stopped() is False


def test_telemetria_fresca_sotto_soglia_blocca(monkeypatch, tmp_path):
    """Controprova: la stessa telemetria FRESCA blocca davvero."""
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setattr(odds_api, "_credit_probe_logged", False)
    _credit_cache(tmp_path, "soccer_epl", 1, time.time())
    assert odds_api.credits_hard_stopped() is True


def test_telemetria_senza_timestamp_non_blocca(monkeypatch, tmp_path):
    """Formato vecchio senza timestamp: eta' ignota -> non si blocca (non si
    puo' sapere se il valore e' obsoleto)."""
    import json
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_probe_logged", False)
    (tmp_path / "toa_soccer_epl.json").write_text(json.dumps(
        {"payload": [], "remaining": 2}))
    assert odds_api.credits_hard_stopped() is False


def test_hard_stop_blocca_la_rotazione_quote(monkeypatch, tmp_path):
    """La rotazione ridotta NON basta: a 4 crediti `_get_odds` non chiama
    l'API (nessun 429 nei log) e ritorna vuoto come fa `should_query_sport`."""
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    # La telemetria crediti vive NELLA cache quote: usiamo un'ALTRA lega per
    # non servire la richiesta dalla cache (che eviterebbe l'HTTP comunque).
    _credit_cache(tmp_path, "soccer_italy_serie_a", 4, time.time())
    monkeypatch.setattr(odds_api.requests, "get", _no_http)
    payload, remaining = odds_api._get_odds(
        "soccer_epl", "2026-09-21", "2026-09-28")
    assert payload == [] and remaining == 0


def test_hard_stop_controprova_sopra_soglia_la_rotazione_chiama(
        monkeypatch, tmp_path):
    """Controprova: a 50 crediti la stessa chiamata parte davvero (il blocco
    e' la soglia, non altro)."""
    import time
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    _credit_cache(tmp_path, "soccer_italy_serie_a", 50, time.time())
    calls = {}

    class _Resp:
        status_code = 200
        headers = {"x-requests-remaining": "49"}

        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": "m1"}]

    def _get(*a, **k):
        calls["n"] = calls.get("n", 0) + 1
        return _Resp()

    monkeypatch.setattr(odds_api.requests, "get", _get)
    payload, remaining = odds_api._get_odds(
        "soccer_epl", "2026-09-21", "2026-09-28")
    assert calls["n"] == 1 and remaining == 49


def test_hard_stop_settlement_usa_la_cache_senza_http(monkeypatch, tmp_path):
    """Il settlement non chiama l'API sotto soglia: usa i punteggi gia' in
    cache (mai dati inventati); il blocco vale anche per il referto."""
    import json
    import time
    from datetime import datetime, timedelta, timezone
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    _credit_cache(tmp_path, "soccer_epl", 3, time.time())
    old = (datetime.now(timezone.utc)
           - timedelta(hours=odds_api.STALE_INPLAY_HOURS + 2)).isoformat()
    cached = [{"id": "m1", "completed": False, "commence_time": old,
               "scores": None}]
    (tmp_path / "toa_scores_soccer_epl.json").write_text(json.dumps(
        {"ts": time.time(), "payload": cached, "remaining": 3,
         "remaining_ts": time.time()}))
    monkeypatch.setattr(odds_api.requests, "get", _no_http)
    assert odds_api.fetch_scores("soccer_epl") == cached


def test_hard_stop_settlement_controprova_sopra_soglia(monkeypatch, tmp_path):
    """Controprova: a 50 crediti il refresh dei punteggi parte."""
    import json
    import time
    from datetime import datetime, timedelta, timezone
    odds_api = _credit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(odds_api, "_credit_stop_logged", False)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    _credit_cache(tmp_path, "soccer_epl", 50, time.time())
    old = (datetime.now(timezone.utc)
           - timedelta(hours=odds_api.STALE_INPLAY_HOURS + 2)).isoformat()
    (tmp_path / "toa_scores_soccer_epl.json").write_text(json.dumps(
        {"ts": time.time(), "remaining": 50,
         "payload": [{"id": "m1", "completed": False,
                      "commence_time": old, "scores": None}]}))
    calls = {}

    class _Resp:
        status_code = 200
        headers = {"x-requests-remaining": "49"}

        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": "m2", "completed": True}]

    def _get(*a, **k):
        calls["n"] = calls.get("n", 0) + 1
        return _Resp()

    monkeypatch.setattr(odds_api.requests, "get", _get)
    assert odds_api.fetch_scores("soccer_epl") == [{"id": "m2",
                                                    "completed": True}]
    assert calls["n"] == 1
