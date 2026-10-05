import { defineRailway, fn, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const betting_botVolume = volume("betting_bot-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 500 });
  // 01/10/2026 — SBLOCCO ORDINI SX: il container in `sfo` (US) riceve 403 da
  // SX Bet su /orders-v3 ("SX Bet is not available in the United States or
  // other prohibited jurisdictions"). La vecchia infrastruttura girava in
  // `ams` (Amsterdam) e da li' gli ordini passavano (bet #41 del 15/09).
  // Questo volume e' la destinazione della migrazione: si crea PRIMA, si
  // copiano i dati, poi si sposta il servizio (il vecchio volume resta
  // dichiarato finche' la migrazione non e' verificata, cosi' `config apply`
  // non lo distrugge).
  const betting_botVolumeAms = volume("betting_bot-volume-ams", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "ams", sizeMB: 500 });
  // ⚠️ REGIONE `ams` (Amsterdam) OBBLIGATORIA dal 01/10/2026: da `sfo` (US)
  // SX Bet risponde 403 su /orders-v3 ("not available in the United States")
  // e il bot non piazza NESSUN ordine. Il volume attivo e' la copia in `ams`;
  // `betting_bot-volume` (sfo) resta dichiarato solo come rete di sicurezza
  // della migrazione: NON rimuoverlo dalla lista risorse finche' la copia in
  // ams non e' considerata definitiva (rimuoverlo la DISTRUGGE).
  const betting_bot = service("betting_bot", {
    source: github("siryo009/betting_bot", { checkSuites: false }),
    replicas: { "ams": 1 },
    networking: { privateNetworkEndpoint: "bettingbot" },
    volumeMounts: { "/app/data": betting_botVolumeAms },
    // ⚠️ Ogni variabile impostata da dashboard/CLI DEVE restare dichiarata con
    // preserve(): `railway config apply` distrugge cio' che non trova nel
    // file. La rigenerazione del 27/09 (`config pull`) aveva perso queste
    // dichiarazioni (i tripwire `test_*_env_dichiarate_nella_iac` le
    // difendono). preserve() non crea valori: le variabili non presenti
    // restano assenti e valgono i default di codice.
    env: {
      ADMIN_CHAT_ID: preserve(),
      ADAPTIVE_WEIGHTING: preserve(),
      ADAPTIVE_WEIGHTING_CLV_FLOOR_THRESHOLD: preserve(),
      ADAPTIVE_WEIGHTING_FLOOR: preserve(),
      ADAPTIVE_WEIGHTING_MIN_SAMPLES: preserve(),
      ADAPTIVE_WEIGHTING_RESTRICT_THRESHOLD: preserve(),
      ADAPTIVE_WEIGHTING_TTL: preserve(),
      ADAPTIVE_WEIGHTING_WINDOW_DAYS: preserve(),
      API_FOOTBALL_KEY: preserve(),
      AUTO_BET_DRY_RUN: preserve(),
      AUTO_BET_MODE: preserve(),
      AUTO_BET_STAKE_MODE: preserve(),
      // Rotazione degli snapshot di backup (03/10/2026): ogni snapshot copia
      // TUTTA `data/`, quindi con i JSONL di telemetria il volume si riempiva
      // (342 MB su 434, 81%). Portato a 2 e dichiarato qui perche' un
      // `config apply` non lo riporti al default di codice (7).
      BACKUP_KEEP: preserve(),
      // Diagnostica crediti + rotazione log (03/10/2026): la telemetria delle
      // chiamate the-odds-api (`credit_diagnose.py`) e la rotazione automatica
      // dei JSONL di telemetria (`telemetry_logs.py`, agganciata al backup
      // giornaliero). Dichiarate cosi' un `config apply` non le distrugge e
      // l'operatore puo' tarare path e soglie senza redeploy di codice.
      CREDIT_CALLS_LOG: preserve(),
      ORACLE_SKIP_LOG: preserve(),
      TELEMETRY_ROTATE_ENABLED: preserve(),
      LOG_ROTATE_MAX_MB: preserve(),
      LOG_ROTATE_AFTER_DAYS: preserve(),
      LOG_ROTATE_QUIET_MIN: preserve(),
      LOG_ROTATE_KEEP: preserve(),
      BOOK_FLOW_DEDUP_MIN: preserve(),
      BOOK_FLOW_LOG: preserve(),
      BOOK_FLOW_MAX_KEYS: preserve(),
      BOOK_FLOW_MIN_JUMP_PCT: preserve(),
      BOOK_FLOW_MIN_SIZE_USDC: preserve(),
      BOOK_FLOW_STATE: preserve(),
      CHIEF_EXECUTION: preserve(),
      // Direttiva "Sblocco Totale LIVE" (30/09/2026): shadow mode SPENTA in
      // produzione. I due interruttori restano dichiarati cosi' `config apply`
      // non li distrugge e l'operatore puo' riaccenderli per una misura.
      CHIEF_SHADOW_ENABLED: preserve(),
      DECISION_SHADOW: preserve(),
      DECISION_SHADOW_PERSIST: preserve(),
      ENABLE_LIVE_OU: preserve(),
      EXA_API_KEY: preserve(),
      EXECUTION_PROVIDER: preserve(),
      GOOGLE_API_KEY: preserve(),
      HEDGE_FRACTION: preserve(),
      HEDGE_HORIZON_H: preserve(),
      HEDGE_LOG: preserve(),
      HEDGE_MAX_STAKE_USDC: preserve(),
      HEDGE_MIN_LOCK_PCT: preserve(),
      HEDGE_MIN_MINUTES: preserve(),
      HEDGE_MIN_MOVE_PCT: preserve(),
      HEDGE_MIN_STAKE_USDC: preserve(),
      KELLY_MAX_FRACTION: preserve(),
      KELLY_MIN_FRACTION: preserve(),
      // Gate di lega dinamico (29/09/2026, `league_dynamic.py`): misura per
      // lega dal ledger, default OFF (sola telemetria) e in ogni caso
      // autorizzato solo a RESTRINGERE. Senza queste voci un `config apply`
      // distruggerebbe le soglie impostate dall'operatore.
      LEAGUE_DYNAMIC_ENABLED: preserve(),
      LEAGUE_DYNAMIC_SINCE: preserve(),
      LEAGUE_DYNAMIC_MIN_SAMPLES: preserve(),
      LEAGUE_DYNAMIC_DEMOTE_ROI: preserve(),
      LEAGUE_DYNAMIC_PROMOTE_ROI: preserve(),
      LEAGUE_DYNAMIC_TTL: preserve(),
      // Intel live (29/09/2026, `live_intel.py` + `agents/data_agent.py`):
      // interruttore, cache e guardia di rete. I TTL per provider sono uno per
      // libreria (fbref/elo via soccerdata, news via ddgs, mlb, nba_api).
      LIVE_INTEL: preserve(),
      LIVE_INTEL_CACHE: preserve(),
      LIVE_INTEL_TIMEOUT_S: preserve(),
      LIVE_INTEL_TTL_ELO: preserve(),
      LIVE_INTEL_TTL_FBREF: preserve(),
      LIVE_INTEL_TTL_MLB: preserve(),
      LIVE_INTEL_TTL_NBA: preserve(),
      LIVE_INTEL_TTL_NEWS: preserve(),
      ODDS_API_KEY: preserve(),
      ODDS_DAILY_BUDGET: preserve(),
      // Oracolo eSports (29/09/2026, `esports_oracle.py`): chiave OddsPapi
      // impostata DIRETTAMENTE dal proprietario su Railway (regola 7, mai in
      // chat). Facoltative: base URL, bookmaker sharp, metodo de-vig.
      ODDSPAPI_KEY: preserve(),
      ODDSPAPI_BASE: preserve(),
      ODDSPAPI_BOOK: preserve(),
      ESPORTS_DEVIG_METHOD: preserve(),
      // Corsia eSports (30/09/2026, `esports_lane.py`): interruttore,
      // identita' dei mercati SX (sport 9 / type 52), finestra, volumi e
      // BUDGET richieste OddsPapi (piano free 250/mese: senza il tetto
      // giornaliero il giro ogni 60s esaurirebbe la quota in poche ore).
      ESPORTS_LIVE: preserve(),
      ESPORTS_SX_SPORT_ID: preserve(),
      ESPORTS_SX_TYPE_ID: preserve(),
      ESPORTS_HOURS_AHEAD: preserve(),
      ESPORTS_MAX_EVENTS: preserve(),
      ESPORTS_MAX_MARKETS: preserve(),
      ESPORTS_REQ_BUDGET_DAY: preserve(),
      // Telemetria ombra mercati SX NON calcistici (01/10/2026,
      // `market_shadow.py`): scrive ESCLUSIVAMENTE su `market_quotes` (mai
      // `predictions`). Spenta di default; queste voci esistono perche' un
      // `config apply` non deve distruggere l'interruttore/copertura che
      // l'operatore imposta.
      SHADOW_MARKET_ENABLED: preserve(),
      SHADOW_SPORTS: preserve(),
      SHADOW_TYPES: preserve(),
      SHADOW_HOURS_AHEAD: preserve(),
      SHADOW_MAX_MARKETS: preserve(),
      SHADOW_GATEWAY_ID: preserve(),
      // Fascia quota della corsia eSports: se assente vale quella della
      // strategia di calcio (1.30-1.80, letta da `value_filter`), quindi
      // `config apply` non deve poterla distruggere se viene impostata.
      ESPORTS_ODDS_MIN: preserve(),
      ESPORTS_ODDS_MAX: preserve(),
      // Finestra dell'oracolo e pacing: su eSports Pinnacle pubblica tardivo e
      // il free tier limita le chiamate al minuto (429 = richiesta persa).
      ESPORTS_ORACLE_WINDOW_H: preserve(),
      ESPORTS_MIN_INTERVAL_S: preserve(),
      ESPORTS_FIXTURES_TTL_MIN: preserve(),
      ESPORTS_ODDS_TTL_MIN: preserve(),
      ESPORTS_ODDS_MISS_TTL_MIN: preserve(),
      ESPORTS_CACHE: preserve(),
      // KELLY DINAMICO (04/10/2026 sera, direttiva "Ottimizzazione
      // Quantitativa" punto 2): k non piu' fisso a 0.65 ma nella BANDA
      // 0.15-0.25 scalata da EV/edge/confidenza della lega
      // (`stake_engine.dynamic_kelly_fraction`), con cap DINAMICO 12% del
      // bankroll e ticket minimo **1.00 USDC** del MOTORE (= floor
      // dell'EXCHANGE SX). `KELLY_AGGRESSIVE_ENABLED=0` ripristina il percorso
      // storico; `ORDER_FIXED_STAKE_USDC` > 0 ripristina l'importo fisso del
      // 28/09. `KELLY_AGGRESSIVE_FRACTION` e' il MASSIMO della banda,
      // `KELLY_AGGRESSIVE_MIN_FRACTION` il minimo.
      KELLY_AGGRESSIVE_FRACTION: preserve(),
      KELLY_AGGRESSIVE_MIN_FRACTION: preserve(),
      KELLY_MAX_STAKE_PCT: preserve(),
      KELLY_MIN_TICKET_USDC: preserve(),
      KELLY_AGGRESSIVE_ENABLED: preserve(),
      // Tre agenti (04/10/2026): Analisi (Steam Velocity + Juice/overround,
      // freschezza delle osservazioni) e Cervello (EV dinamico su liquidita'
      // e volatilita' + Portfolio Shield anti-correlazione).
      ANALYSIS_MAX_AGE_S: preserve(),
      ANALYSIS_JUICE_STATE: preserve(),
      ANALYSIS_JUICE_SPIKE_PP: preserve(),
      BRAIN_MIN_DEPTH_USDC: preserve(),
      BRAIN_EV_MULT_LIQUIDITY: preserve(),
      BRAIN_EV_MULT_VOLATILITY: preserve(),
      BRAIN_VOLATILITY_PP_MIN: preserve(),
      BRAIN_SHIELD_CAP_PCT: preserve(),
      OPEN_EXPOSURE_CAP_PCT: preserve(),
      ORDER_FIXED_STAKE_USDC: preserve(),
      ORDER_MAX_STAKE_USDC: preserve(),
      T60_MAX_STAKE_USDC: preserve(),
      // Finestra esecutiva **T-180..T-2** (04/10/2026, direttiva
      // "Ottimizzazione Quantitativa" punto 3): era T-60..T-5. L'apertura a
      // 3 ore cattura le formazioni ufficiali e i volumi dei sindacati
      // quantitativi; la chiusura a 2 minuti e' ALLINEATA al pavimento
      // assoluto `MIN_MINUTES_TO_START` (2) — le due guardie devono
      // coincidere, altrimenti l'ultima fascia e' una zona morta silenziosa.
      // Le env sono l'unico modo di tararla senza redeploy.
      T60_WINDOW_MIN_MIN: preserve(),
      T60_WINDOW_MAX_MIN: preserve(),
      // Interruttori della strategia T-60: `T60_EXECUTION_ONLY=0` ripristina
      // l'orizzonte aperto (usato da test e diagnostica) e
      // `T60_ORDER_VALIDATION=0` spegne il contratto CB3 sui payload.
      T60_EXECUTION_ONLY: preserve(),
      T60_ORDER_VALIDATION: preserve(),
      OU_LIVE_MIN_CLOSURES: preserve(),
      // Gate EV dei MERCATI LIQUIDI (04/10/2026, punto 4): AH/OU/Totals/BTTS/ML
      // usano una soglia dedicata (default 1.0%) mentre 1X2/eSports/tennis
      // restano a `value_filter.EV_MIN`. `EV_LIQUID_MARKETS` e' il CSV dei
      // mercati a cui applicarla (una sola definizione, in `value_filter`).
      EV_MIN_LIQUID: preserve(),
      EV_LIQUID_MARKETS: preserve(),
      // Closing line a T-0 (04/10/2026, punto 5): la routine `closing_line.py`
      // scrive `clv_history.closing_odds` DENTRO la finestra T-PRE..T+POST
      // rispetto al kickoff (default 10' prima / 5' dopo). Sola lettura dalle
      // cache sharp: zero crediti, zero ordini.
      CLOSING_LINE_ENABLED: preserve(),
      CLOSING_LINE_PRE_MIN: preserve(),
      CLOSING_LINE_POST_MIN: preserve(),
      CLOSING_LINE_MAX_ROWS: preserve(),
      // Oracolo top-down (25/09/2026): `PINNACLE_CONSENSUS=0` (03/10/2026)
      // usa la SOLA Pinnacle de-vigata con Shin — Betfair e Matchbook fuori,
      // per togliere lag e rumore dal confronto. `TOP_DOWN_BYPASS` resta 0: il
      // filtro fascia 1.30-1.80 e' il guardrail, non un'opzione.
      PINNACLE_CONSENSUS: preserve(),
      PINNACLE_CONSENSUS_METHOD: preserve(),
      PINNACLE_VALIDATOR_TOLERANCE: preserve(),
      // De-vig dell'oracolo (02/10/2026): default di codice = "shin" (Shin
      // 1992/93, parametro z di denaro informato). Rollback a "power" con
      // questa env, senza redeploy di codice.
      PINNACLE_DEVIG_METHOD: preserve(),
      PINNACLE_CACHE_MAX_AGE_H: preserve(),
      // TTL DINAMICA della cache oracolo per tempo al kickoff (05/10/2026,
      // direttiva del proprietario): T > 180' -> 30', T-60..T-180 -> 5',
      // T < 60' -> 2'. La formula vive in `pinnacle_oracle.cache_ttl_minutes`
      // (gate + scheduler); queste env ne tarano solo i VALORI. Senza
      // preserve(), un `config apply` distruggerebbe la taratura.
      PINNACLE_TTL_LONG_MIN: preserve(),
      PINNACLE_TTL_MID_MIN: preserve(),
      PINNACLE_TTL_SHORT_MIN: preserve(),
      // Steam move sullo sharp (02/10/2026, `steam_move.py`): ΔQ/Δt della
      // quota Pinnacle sugli ultimi 15-30', crollo >= 4% = priorita'
      // d'esecuzione prima che SX riallinei. Senza preserve(), un `config
      // apply` distruggerebbe gli override operativi.
      STEAM_MOVE_ENABLED: preserve(),
      STEAM_MOVE_PCT: preserve(),
      STEAM_MOVE_WINDOW_MIN: preserve(),
      STEAM_MOVE_MIN_WINDOW_MIN: preserve(),
      STEAM_MOVE_BOOK: preserve(),
      STEAM_MOVE_DEDUP_MIN: preserve(),
      QUOTAVERACE_BOT_TOKEN: preserve(),
      SETTLEMENT_HEAL_INTERVAL_HOURS: preserve(),
      SMART_HEDGING: preserve(),
      STAKE_CAP_HARD: preserve(),
      STAKE_CAP_PCT: preserve(),
      STAKE_CAP_PCT_STRONG: preserve(),
      SX_API_KEY: preserve(),
      SX_BOOK_LEVELS_KEPT: preserve(),
      SX_PRIVATE_KEY: preserve(),
      T60_KILL_WALLET_USDC: preserve(),
      // (la FINESTRA esecutiva ha le sue env dichiarate piu' sopra, nel
      // blocco dei circuit breakers T-60: T-180..T-2 dal 04/10/2026.)
      TENNIS_SANDBOX_ENABLED: preserve(),
      // Corsia TENNIS (30/09/2026, `tennis_lane.py`): interruttore, identita'
      // SX (sport 6 / type 52), soglia EV PROPRIA (2.5%), FASCIA QUOTA della
      // corsia (1.30-2.50 dal 02/10/2026: oltre la banda il longshot non e' un
      // edge ma varianza — riapplicata anche in `auto_bet._tennis_picks`),
      // finestra, budget richieste the-odds-api (1 credito/torneo con cache
      // scaduta) e stato. Gli ORDINI sono nella corsia LIVE di `auto_bet`.
      TENNIS_LANE: preserve(),
      TENNIS_EV_MIN: preserve(),
      TENNIS_ODDS_MIN: preserve(),
      TENNIS_ODDS_MAX: preserve(),
      TENNIS_HOURS_AHEAD: preserve(),
      TENNIS_MAX_MARKETS: preserve(),
      TENNIS_MIN_INV_SUM: preserve(),
      TENNIS_MAX_INV_SUM: preserve(),
      TENNIS_ORACLE_TTL_MIN: preserve(),
      TENNIS_REQ_BUDGET_DAY: preserve(),
      TENNIS_ORACLE_CACHE: preserve(),
      TENNIS_LANE_STATE: preserve(),
      TENNIS_JOB_INTERVAL_MIN: preserve(),
      TENNIS_DISCOVERY_TTL_S: preserve(),
      // Motore quantitativo TENNIS (03/10/2026, `tennis_quant.py`): ELO
      // superficie-specifico + Poisson da hold/break in parallelo al de-vig
      // di Shin. Modalita' SOLO MISURA (nessun ordine: lo stake reale resta
      // `auto_bet.order_stake`). Dichiarate perche' un `config apply` non
      // deve distruggere soglie/parametri che l'operatore tara.
      TENNIS_QUANT_ENABLED: preserve(),
      TENNIS_QUANT_DB: preserve(),
      TENNIS_QUANT_LOG: preserve(),
      TENNIS_QUANT_EV_MIN: preserve(),
      TENNIS_QUANT_W_ELO: preserve(),
      TENNIS_QUANT_RETURN_GAMES: preserve(),
      TENNIS_QUANT_MAX_BREAKS: preserve(),
      TENNIS_QUANT_BASE_HOLD: preserve(),
      TENNIS_QUANT_HOLD_SPREAD: preserve(),
      TENNIS_QUANT_KELLY_FRACTION: preserve(),
      TENNIS_QUANT_MAX_STAKE_PCT: preserve(),
      TENNIS_QUANT_MAX_STAKE_ABS: preserve(),
      TENNIS_QUANT_HOURS_AHEAD: preserve(),
      TENNIS_QUANT_MIN_MODEL_MATCHES: preserve(),
      TENNIS_QUANT_SHARP_METHOD: preserve(),
      TEST_NOTIFY_KEY: preserve(),
      TOP_DOWN_EV: preserve(),
      TOP_DOWN_MARGIN: preserve(),
      // Corsia top-down: `0` = il filtro fascia 1.30-1.80 resta il guardrail
      // (confermato dal proprietario il 03/10/2026: lo steam chasing su quote
      // esterne alla banda introduce una varianza che il bankroll non regge).
      TOP_DOWN_BYPASS: preserve(),
      // Oracolo a linea OU/AH (30/09/2026, `line_oracle.py`): follow-the-
      // money — totals/spreads Pinnacle SOLO per le leghe con pick a linea
      // in gioco (3 crediti/chiamata, budget giornaliero dedicato). Senza
      // preserve(), un `config apply` distruggerebbe gli override operativi.
      ORACLE_ENABLED: preserve(),
      ORACLE_BUDGET_DAY: preserve(),
      // Finestra di fetch dell'oracolo a linea (minuti, default 70): si ordina
      // solo nella finestra esecutiva T-60..T-5, quindi non si scarica piu'
      // l'intero palinsesto della lega. Sostituisce ORACLE_PICK_WINDOW_H
      // (orizzonte a ore, rimosso il 03/10/2026).
      ORACLE_FETCH_WINDOW_MIN: preserve(),
      ORACLE_LEAGUES_PER_PASS: preserve(),
      // Fetch ON-DEMAND (05/10/2026, `line_oracle.fetch_for_pick`): quando il
      // gate incontra `no_oracle/EXPIRED_CACHE` su un pick IN FINESTRA paga
      // subito la fetch della SUA lega. Dedup per lega in secondi (default
      // 120): senza, una tornata di pick sulla stessa lega brucerebbe l'intero
      // budget `ORACLE_BUDGET_DAY` (condiviso con lo scheduler) in un attimo.
      ORACLE_ONDEMAND_DEDUP_S: preserve(),
      // Interruttore del fetch on-demand (default ON: spento solo con un valore
      // esplicito 0/false/no/off). Spegne l'UNICA spesa aggiuntiva possibile del
      // gate a linea senza toccare il resto dell'oracolo.
      ORACLE_ONDEMAND_ENABLED: preserve(),
    },
  });

  // Servizio CRON dedicato allo scanner surebet (surebet_engine.py): esegue
  // uno scan singolo e TERMINA a ogni scatto del cron (restartPolicyType
  // NEVER). Volume DEDICATO (il progetto non supporta volumi condivisi fra
  // servizi). Crediti the-odds-api CONDIVISI col value bot: SUREBET_MIN_
  // REMAINING=50 ferma lo scanner sotto soglia per proteggere il budget.
  const surebetData = volume("surebet-volume", { sizeMB: 100, region: "sfo" });
  const surebet = fn("surebet", {
    source: github("siryo009/betting_bot", { checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.surebet" },
    // ⛔ CRON SOSPESO IL 04/10/2026 (direttiva del proprietario).
    // Motivo: il limite di sostenibilita' e' ~14 crediti/giorno e il core
    // business validato e' lo Steam Chasing (oracolo + rotazione ~12): gli 8
    // crediti/giorno del surebet (2 sport x 2 fetch x 2 regioni) portavano il
    // profilo fuori budget senza margine di sicurezza. Il modulo resta nel
    // codice e il servizio/volume restano dichiarati: RIACCENDERE = rimettere
    //   deploy: { cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" }
    //   + SUREBET_ENABLED: "1" (qui sotto)
    //
    // DUE serrature, nessuna delle quali e' il fragile trucco di svuotare
    // SUREBET_SPORTS (un valore vuoto viene trattato come non impostato e il
    // codice ricade sul default NBA+MLB: riaccenderebbe il costo in silenzio).
    //   1. CRON (sopra): la sorgente del costo periodico (~8 cr/giorno).
    //   2. SUREBET_ENABLED=0 (sotto): copre il RESIDUO che il cron non copre
    //      — il servizio ha `source: github(...)`, quindi OGNI push su main lo
    //      fa ripartire ed esegue il CMD una volta anche senza cron (~4 cr a
    //      push). Il run esce subito, zero chiamate.
    // La direzione del fail-safe e' quella giusta per un costo: dimenticare la
    // serratura 2 a "0" con il cron riacceso NON paga nulla e urla nei log; non
    // puo' riaccendere il costo in silenzio.
    deploy: { restartPolicyType: "NEVER" },
    volumeMounts: { "/app/data": surebetData },
    env: {
      ADMIN_CHAT_ID: preserve(),
      // Impostata dall'operatore a livello ambiente (visibile anche a questo
      // servizio): preserve() la protegge senza crearla né usarla.
      ODDSPAPI_KEY: preserve(),
      ODDS_API_KEY: preserve(),
      QUOTAVERACE_BOT_TOKEN: preserve(),
      SUREBET_BUDGET: "100",
      // 01/10/2026: ripristinata la coppia della stagione invernale — NBA al
      // via (verificata `active: true` su /v4/sports) accanto a MLB. Costo:
      // ~4 fetch/sport/giorno col TTL 6h qui sotto (~8 crediti/giorno in
      // totale). Se il credit watchdog segnala pressione: prima si alza
      // SUREBET_ODDS_TTL (12h = meta' del costo), poi si toglie MLB a fine
      // stagione — MAI toccare la rotazione value, che e' la corsia del
      // denaro.
      // 01/10/2026: TTL portato a 12h (era 6h) su direttiva del proprietario:
      // dimezza i fetch del cron (2 invece di 4 per sport al giorno) e rientra
      // nel budget dei 500 crediti/mese senza togliere sport. NB: qui il TTL e'
      // anche quello del codice (`surebet_engine.ODDS_TTL`), quindi un
      // `config apply` non puo' riportarlo a 6h.
      SUREBET_SPORTS: "basketball_nba,baseball_mlb",
      // 04/10/2026: interruttore di servizio OFF (modulo sospeso, vedi il
      // commento sul blocco `deploy`). Dichiarato qui come valore ESPLICITO
      // (non preserve()): cosi' il piano non genera drift e un `config apply`
      // non puo' riaccendere lo scanner. Valori riconosciuti come OFF: 0,
      // false, no, off, disabled, paused.
      SUREBET_ENABLED: "0",
      SUREBET_ODDS_TTL: "43200",
      SUREBET_MIN_REMAINING: "50",
      SUREBET_MIN_MARGIN: "0.005",
      SUREBET_CRON_HOLD_SECONDS: preserve(),
    },
  });

  return project("creative-vibrancy", {
    resources: [betting_bot, betting_botVolume, betting_botVolumeAms, surebet, surebetData],
  });
});
