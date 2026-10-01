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
      OPEN_EXPOSURE_CAP_PCT: preserve(),
      ORDER_FIXED_STAKE_USDC: preserve(),
      ORDER_MAX_STAKE_USDC: preserve(),
      OU_LIVE_MIN_CLOSURES: preserve(),
      PINNACLE_CONSENSUS: preserve(),
      PINNACLE_CONSENSUS_METHOD: preserve(),
      PINNACLE_VALIDATOR_TOLERANCE: preserve(),
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
      T60_WINDOW_MIN_MIN: preserve(),
      // Chiusura della finestra esecutiva (30/09/2026): con la corsia eSports
      // (oracolo tardivo) l'esecuzione deve poter arrivare fino a T-15, quando
      // arrivano i ritentativi utili. Dichiarata perche' e' impostata su
      // Railway: senza, un `config apply` la distruggerebbe.
      T60_WINDOW_MAX_MIN: preserve(),
      TENNIS_SANDBOX_ENABLED: preserve(),
      // Corsia TENNIS (30/09/2026, `tennis_lane.py`): interruttore, identita'
      // SX (sport 6 / type 52), soglia EV PROPRIA (2.5%), finestra, budget
      // richieste the-odds-api (1 credito/torneo con cache scaduta) e stato.
      // Gli ORDINI sono nella corsia LIVE di `auto_bet` (`_tennis_picks`).
      TENNIS_LANE: preserve(),
      TENNIS_EV_MIN: preserve(),
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
      TEST_NOTIFY_KEY: preserve(),
      TOP_DOWN_EV: preserve(),
      TOP_DOWN_MARGIN: preserve(),
      // Oracolo a linea OU/AH (30/09/2026, `line_oracle.py`): follow-the-
      // money — totals/spreads Pinnacle SOLO per le leghe con pick a linea
      // in gioco (3 crediti/chiamata, budget giornaliero dedicato). Senza
      // preserve(), un `config apply` distruggerebbe gli override operativi.
      ORACLE_ENABLED: preserve(),
      ORACLE_BUDGET_DAY: preserve(),
      ORACLE_PICK_WINDOW_H: preserve(),
      ORACLE_LEAGUES_PER_PASS: preserve(),
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
    deploy: { cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" },
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
      SUREBET_SPORTS: "basketball_nba,baseball_mlb",
      SUREBET_ODDS_TTL: "21600",
      SUREBET_MIN_REMAINING: "50",
      SUREBET_MIN_MARGIN: "0.005",
      SUREBET_CRON_HOLD_SECONDS: preserve(),
    },
  });

  return project("creative-vibrancy", {
    resources: [betting_bot, betting_botVolume, betting_botVolumeAms, surebet, surebetData],
  });
});
