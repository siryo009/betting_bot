import { defineRailway, fn, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Volume persistente reale (creato in precedenza via CLI: api-volume).
  // Montato su /app/data (QUOTAVERACE_DATA_DIR). Railway NON supporta volumi
  // condivisi fra servizi, quindi bot + web API convivono nello stesso servizio
  // (entrypoint run_all.py): DB, data/scan_*.json, log e kill-switch sono
  // condivisi di fatto dallo stesso container.
  const data = volume("api-volume", { sizeMB: 500, region: "ams", isCreated: true });

  // Volume DEDICATO allo scanner surebet (cron): cache the-odds-api +
  // log dedup persistono tra un'esecuzione e l'altra (Railway NON supporta
  // volumi condivisi fra servizi → il cron ha il SUO volume).
  const surebetData = volume("surebet-volume", { sizeMB: 100, region: "ams" });

  // Env condiviso: .env locale non viene deployato, quindi i valori vivono in
  // Railway. preserve() mantiene i valori gia' presenti e NON li esporta in
  // chiaro: una variabile non dichiarata qui sarebbe distrutta da config apply.
  const sharedEnv = {
    QUOTAVERACE_BOT_TOKEN: preserve(),
    ODDS_API_KEY: preserve(),
    API_FOOTBALL_KEY: preserve(),
    // Rate limit del piano free API-Football (10 richieste al MINUTO, oltre a
    // 100/giorno): secondi minimi fra due chiamate, letto da
    // football_hist.api_min_interval(). Default di codice 6.5 (≈9/min);
    // preserve() per non distruggerlo se un giorno viene tarato su Railway.
    API_FOOTBALL_MIN_INTERVAL: preserve(),
    BANKROLL_DEFAULT: preserve(),
    ADMIN_CHAT_ID: preserve(),
    TEST_NOTIFY_KEY: preserve(),
    // Esecuzione LIVE (09/09): AUTO_BET_MODE=live + EXECUTION_PROVIDER +
    // credenziali SX abilitano gli ordini reali su SX Bet. preserve()
    // mantiene i valori gia' presenti in Railway senza esportarli: senza
    // queste dichiarazioni un `railway config apply` li distruggerebbe.
    AUTO_BET_MODE: preserve(),
    EXECUTION_PROVIDER: preserve(),
    SX_API_KEY: preserve(),
    SX_PRIVATE_KEY: preserve(),
    // Staking prudente (09/09): Kelly FISSATO al 5% (MIN=MAX=0.05) e cap
    // 2% per singola operazione (value e strong_value). Il floor exchange
    // MIN_STAKE_EUR=1.0 (minimo ordine SX Bet in USDC) resta invariato.
    AUTO_BET_STAKE_MODE: preserve(),
    // Kelly parametrizzabile (25/09): la frazione dinamica si tara da env
    // SENZA toccare la formula. KELLY_BASE_FRACTION e' il punto di partenza
    // (default 0.25), KELLY_MIN/MAX limitano l'intervallo (0.05-0.40);
    // DRAWDOWN_* governano la protezione dal drawdown.
    KELLY_MIN_FRACTION: preserve(),
    KELLY_MAX_FRACTION: preserve(),
    KELLY_BASE_FRACTION: preserve(),
    DRAWDOWN_THRESHOLD: preserve(),
    DRAWDOWN_REDUCTION: preserve(),
    STAKE_CAP_PCT: preserve(),
    STAKE_CAP_PCT_STRONG: preserve(),
    // ⚠️ DUE nomi distinti nel codice: `MIN_STAKE_EUR` (auto_bet = floor
    // dell'exchange, default 1.0) e `STAKE_MIN_EUR` (adaptive_staking =
    // arrotondamento, default 0.01). Prima era dichiarato solo il primo:
    // il secondo, se impostato su Railway, sarebbe stato distrutto da
    // `railway config apply`.
    MIN_STAKE_EUR: preserve(),
    STAKE_MIN_EUR: preserve(),
    STAKE_STEP_EUR: preserve(),
    BET_STAKE_EUR: preserve(),
    // Cap severo (11/09): il floor exchange non puo' alzare lo stake oltre
    // il cap per singola bet. Con STAKE_CAP_HARD=1 (default) una bet il cui
    // stake cappato e' sotto il minimo ordine viene SALTATA: con wallet
    // < 100 USDC il cap 1% non e' sostenibile e il bot non piazza nulla
    // (fail-closed), invece di forzare 1 USDC (= 2.6% su 38 USDC).
    STAKE_CAP_HARD: preserve(),
    // Strategia T-60 + circuit breakers (17/09): finestra esecutiva T-60..T-50
    // (T60_EXECUTION_ONLY=0 ripristina l'orizzonte 0.5-24h), CB1 hard cap per
    // ordine (1.00 USDC = minimo eseguibile SX, scelta del proprietario;
    // T60_MAX_STAKE_USDC=0.50 torna alla direttiva letterale ma NESSUN ordine
    // puo' partire sotto il floor exchange), CB2 kill switch patrimoniale a
    // 30 USDC di EQUITY (flag persistente + alert Telegram, reset /t60reset),
    // CB3 validazione Pydantic rigida del payload d'ordine.
    T60_EXECUTION_ONLY: preserve(),
    T60_WINDOW_MIN_MIN: preserve(),
    T60_WINDOW_MAX_MIN: preserve(),
    T60_MAX_STAKE_USDC: preserve(),
    T60_MAX_ODDS: preserve(),
    T60_KILL_WALLET_USDC: preserve(),
    // Pivot top-down fase 2 (25/09): l'EV del giro ordini si calcola
    // sull'oracolo Pinnacle letto DALLE CACHE (zero crediti) invece che sul
    // modello; DRY-RUN intercetta l'ordine prima del POST a SX Bet.
    TOP_DOWN_EV: preserve(),
    TOP_DOWN_MARGIN: preserve(),
    // Consenso multi-oracolo (26/09/2026): la p_true del gate top-down non e'
    // piu' il prezzo secco di una sola fonte. PINNACLE_CONSENSUS=0 ripristina
    // la Pinnacle-secca; il metodo (mean|median) e la tolleranza del
    // validatore Matchbook sono tarabili senza redeploy di codice.
    PINNACLE_CONSENSUS: preserve(),
    PINNACLE_CONSENSUS_METHOD: preserve(),
    PINNACLE_VALIDATOR_TOLERANCE: preserve(),
    PINNACLE_DEVIG_METHOD: preserve(),
    // Corsia top-down (25/09, direttiva "bypass del filtro quote"): la corsia
    // LIVE pesca da ogni riga 1X2 (qualsiasi quota/status) e l'unico giudice
    // del prezzo e' l'oracolo Pinnacle (fail-closed senza oracolo).
    // ⚠️ DEFAULT 0 (SPENTO) dal 26/09: il bypass vale solo con questa env a 1;
    // `preserve()` la protegge da `config apply`, non la accende.
    TOP_DOWN_BYPASS: preserve(),
    // Extra EV per le leghe in probation sulla corsia top-down (default 2pp).
    TOP_DOWN_PROBATION_EXTRA: preserve(),
    AUTO_BET_DRY_RUN: preserve(),
    T60_ORDER_VALIDATION: preserve(),
    // Multi-mercato OU/AH (19/09): ENABLE_LIVE_AH=1 -> l'Asian Handicap
    // piazza ORDINI REALI; ENABLE_LIVE_OU=0 (default di codice) -> l'Over/Under
    // resta in shadow/telemetria (il leak storico -6.8% sul mercato OU va
    // rimisurato sulla corsia nuova prima di rimetterci denaro). preserve()
    // perche' `railway config apply` non deve spegnere l'AH per sbaglio.
    ENABLE_LIVE_AH: preserve(),
    // AUTORIZZAZIONE ≠ ORDINI (26/09/2026): con ENABLE_LIVE_OU=1 l'OU e'
    // autorizzato, ma ordina solo quando il gate di PRONTEZZA lo abilita
    // (>= OU_LIVE_MIN_CLOSURES chiusure giocabili dell'era e ROI positivo).
    ENABLE_LIVE_OU: preserve(),
    OU_LIVE_MIN_CLOSURES: preserve(),
    OU_LIVE_SINCE: preserve(),
    OU_READY_TTL: preserve(),
    // Finestra/limiti della corsia multi-mercato. Default di codice dal
    // 26/09: 24h, 20 linee per mercato, 600 mercati per discovery (prima
    // 12/400). Si allarga solo la COPERTURA OU/AH: fascia quota, edge, EV e
    // gate di lega restano quelli congelati del 22/09. MM_ENABLED=0 spegne
    // l'intero job multi-mercato.
    MM_ENABLED: preserve(),
    MM_HOURS_AHEAD: preserve(),
    MM_MAX_LINES_PER_MARKET: preserve(),
    MM_MAX_RAW_MARKETS: preserve(),
    MM_GATEWAY_ID: preserve(),
    // Flusso dell'order book SX (26/09): rilevatore di INGRESSI di liquidita'
    // sui book GIA' scaricati da sx_signals/multi_market. TELEMETRIA — non
    // piazza ordini e non tocca i gate di strategia.
    // SX_BOOK_LEVELS_KEPT = livelli conservati per esito in `sx_signals._book`.
    SX_BOOK_LEVELS_KEPT: preserve(),
    BOOK_FLOW_MIN_SIZE_USDC: preserve(),
    BOOK_FLOW_MIN_JUMP_PCT: preserve(),
    BOOK_FLOW_DEDUP_MIN: preserve(),
    BOOK_FLOW_MAX_KEYS: preserve(),
    BOOK_FLOW_STATE: preserve(),
    BOOK_FLOW_LOG: preserve(),
    // Pausa settlement (11/09): con SETTLEMENT_PAUSED=1 (o il flag su volume
    // data/execution/settlement_paused.json) i settle non chiudono nulla e
    // _update_results non scarica risultati (zero crediti).
    SETTLEMENT_PAUSED: preserve(),
    // Tetto giornaliero di chiamate quote (default di codice 12): le leghe in
    // eccesso vengono rinviate al giorno dopo. In produzione era stato messo a
    // 2 durante la crisi crediti del 12-21/09; dal 21/09 e' a **8** (direttiva
    // "volume"): era il VERO collo di bottiglia del flusso (2 leghe/giorno
    // analizzate = board quasi vuota), mentre i gate di lega e le soglie di
    // edge/liquidita' bloccavano ~0 candidati nelle ultime 24h.
    ODDS_DAILY_BUDGET: preserve(),
    // Refertazione: finestra di query (3gg, allineata a /scores) e intervallo
    // della VERIFICA PERIODICA delle leghe senza righe aperte (36h -> 48h dal
    // 21/09, per fare spazio ai crediti della rotazione quote).
    SETTLEMENT_WINDOW_DAYS: preserve(),
    SETTLEMENT_HEAL_INTERVAL_HOURS: preserve(),
    // Guardrail di rischio (11/09): stop-loss giornaliero (-5% per 24h) e
    // filtro liquidita' SX (profondita' taker minima del match e del singolo
    // esito). preserve() per non farli distruggere da `railway config apply`.
    DAILY_STOP_LOSS_PCT: preserve(),
    DAILY_STOP_HOURS: preserve(),
    // Taratura liquidita' SX (11/09, secondo giro): profondita' totale del
    // match, minimo per esito, minimo della LEG GIOCATA (usata sia dallo
    // scan sia dal guardrail d'ordine) e margine richiesto sullo stake
    // (stake x multiplo, cosi' l'ordine non esaurisce il lato del book).
    SX_MIN_DEPTH_USDC: preserve(),
    SX_MIN_LEG_DEPTH_USDC: preserve(),
    SX_MIN_EXEC_DEPTH_USDC: preserve(),
    SX_DEPTH_MULTIPLIER: preserve(),
    // Monitor scarti liquidita' (11/09): path del JSONL (default sul volume
    // data/execution/liquidity_skips.jsonl) e finestra di allerta delle
    // soglie usate dal report.
    LIQUIDITY_SKIP_LOG: preserve(),
    // Sandbox tennis (14/09): TENNIS_SANDBOX_ENABLED=1 è impostata a mano su
    // Railway e NON era dichiarata qui → un `railway config apply` l'avrebbe
    // CANCELLATA (spegnendo scan+settle del tennis). Dichiarata con preserve()
    // come le altre; i limiti di staking (default di codice 0.10 / 0.02 / 25)
    // sono elencati per lo stesso motivo, cosi' non spariscono se un giorno
    // vengono tarati dall'esterno.
    TENNIS_SANDBOX_ENABLED: preserve(),
    TENNIS_KELLY_FRACTION: preserve(),
    TENNIS_MAX_STAKE_PCT: preserve(),
    TENNIS_MAX_STAKE_ABS: preserve(),
    // BETFAIR_* rimosse il 04/09: Betfair è fuori dall'architettura
    // (refertazione = API-Football, quote/CLV = the-odds-api, auto_bet SIM).
  };

  // Workflow di ricerca agentico (research_graph/, 14/09): adapter reali Exa
  // (ricerca web) + Gemini (validazione semantica). Le credenziali stanno SOLO
  // sul servizio `api` (il cron surebet non ne ha bisogno: privilegio minimo).
  // preserve() come per le altre: senza dichiarazione un `railway config apply`
  // le distruggerebbe. Manopole: EXA_SEARCH_TYPE (default `neural`, l'unico
  // tipo Exa che restituisce `score` => confidence reale dei finding),
  // RESEARCH_LLM_MODEL (default gemini-3.6-flash) e RESEARCH_TRACE_DIR
  // (default DATA_DIR/research).
  const researchEnv = {
    EXA_API_KEY: preserve(),
    GOOGLE_API_KEY: preserve(),
    EXA_SEARCH_TYPE: preserve(),
    RESEARCH_LLM_MODEL: preserve(),
    RESEARCH_TRACE_DIR: preserve(),
  };

  // Catena di decisione (decision/, 14-15/09) e GATEWAY DI MERCATO
  // (decision/feeds.py): la sorgente primaria e' SX Bet (letture PUBBLICHE:
  // nessuna credenziale, zero crediti the-odds-api). Solo sul servizio `api`
  // (il cron surebet non usa il pacchetto: privilegio minimo).
  // Manopole: DECISION_FEED_ENABLED (default ON: il gate di mercato e'
  // fail-closed e tiene ferme le puntate finche' il feed non e' validato),
  // DECISION_FEED_PRIMARY (default sxbet), DECISION_FEED_MAX_AGE_MIN (20),
  // DECISION_FEED_MIN_REFRESHES (3), DECISION_FEED_REFRESH_MIN_SEC (600,
  // finestra di riuso: il job gira ogni 60s e l'exchange va rispettato),
  // DECISION_FEED_STATE (default DATA_DIR/decision/feed_state.json).
  // preserve(): una variabile non dichiarata sarebbe distrutta da un
  // `railway config apply`. Tutte le letture trattano la stringa vuota come
  // "default di codice", quindi dichiararle non cambia il comportamento.
  const decisionEnv = {
    DECISION_FEED_ENABLED: preserve(),
    DECISION_FEED_PRIMARY: preserve(),
    DECISION_FEED_MAX_AGE_MIN: preserve(),
    DECISION_FEED_MIN_REFRESHES: preserve(),
    DECISION_FEED_REFRESH_MIN_SEC: preserve(),
    DECISION_FEED_STATE: preserve(),
    DECISION_SHADOW: preserve(),
    DECISION_LOG_SINK: preserve(),
    DECISION_LOG_MAX_MB: preserve(),
    DECISION_SHADOW_LOG: preserve(),
    DECISION_OBSERVABILITY: preserve(),
    DECISION_MIN_MODEL_COVERAGE: preserve(),
    DECISION_REVIEW_ENABLED: preserve(),
    DECISION_REVIEW_QUEUE: preserve(),
    DECISION_REVIEW_CONFIDENCE: preserve(),
    // Revisioni su Telegram (15/09/2026): coda dei verdetti `review`, store dei
    // callback idempotenti e spegnitore della coda stessa. `DECISION_REVIEWS=0`
    // disattiva il riempimento della coda (nessun prompt), senza toccare la
    // catena: e' l'unico interruttore che serve per silenziare gli operatori.
    DECISION_REVIEWS: preserve(),
    DECISION_CALLBACK_STORE: preserve(),
    // Shadow Validation (16/09/2026): con `DECISION_SHADOW_PERSIST=1` la shadow
    // mode PERSISTE la valutazione sul ledger `decisions` (stato `pending`), la
    // convalida subito e blocca l'ordine a convalida non positiva. Default di
    // codice OFF: senza dichiarazione, un `config apply` la distruggerebbe (e
    // con essa la scelta, in entrambe le direzioni).
    DECISION_SHADOW_PERSIST: preserve(),
    // Percorso CLV laterale (17/09/2026): `evaluate_clv` -> `WriteCLVCommand` ->
    // `ClvGateway` in parallelo alla catena, writer sul registro shadow (ZERO
    // scritture su `clv_history`: il CLV di produzione resta di fixture_engine).
    // `DECISION_CLV_SHADOW=0` lo spegne. Default di codice ON (misura senza
    // effetti); la dichiarazione protegge la scelta in entrambe le direzioni.
    DECISION_CLV_SHADOW: preserve(),
    // Confronto shadow catena ↔ corsia (16/09/2026): job ogni 6h che mette a
    // confronto il ledger delle decisioni con quello delle puntate (sola
    // lettura, zero costi). `DECISION_COMPARE_ENABLED=0` spegne il job;
    // `DECISION_COMPARE_DAYS` cambia la finestra (default 7 giorni).
    DECISION_COMPARE_ENABLED: preserve(),
    DECISION_COMPARE_DAYS: preserve(),
  };

  const api = service("api", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "ams": 1 },
    volumeMounts: { ["/app/data"]: { type: "volume", name: data.name, address: data.address } },
    env: { ...sharedEnv, ...researchEnv, ...decisionEnv, RAILWAY_DOCKERFILE_PATH: preserve() },
  });

  // Servizio CRON dedicato allo scanner surebet (surebet_engine.py).
  // Railway esegue lo start command (CMD del Dockerfile.surebet = scan singolo
  // che TERMINA) a ogni scatto del cron — il processo deve uscire a fine task.
  // ⚠️ Crediti the-odds-api: la chiave e' CONDIVISA col value bot (piano free
  // ~500/mese, il calendario value ne usa ~407-460). La FREQUENZA del cron NON
  // determina le chiamate API (le limita il TTL cache SUREBET_ODDS_TTL=6h:
  // max ~8 chiamate/giorno per NBA+MLB) e SUREBET_MIN_REMAINING=50 ferma lo
  // scanner sotto i 50 crediti residui, proteggendo il budget del value bot.
  const surebet = fn("surebet", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.surebet" },
    // restartPolicyType NEVER: il container del cron ESEGUE e DEVE uscire a fine
    // scan (vedi sopra). Era impostato solo sul servizio live, non nel file:
    // senza questa riga un `config apply` lo avrebbe riportato al default
    // (restart su errore = possibile loop di un scan che fallisce).
    deploy: { cronSchedule: "*/15 * * * *", restartPolicyType: "NEVER" },
    volumeMounts: { ["/app/data"]: { type: "volume", name: surebetData.name, address: surebetData.address } },
    env: {
      ...sharedEnv,
      SUREBET_BUDGET: "100",
      // SETTEMBRE 2026 sotto-budget: solo MLB (in stagione, partite ogni
      // giorno = campo-test continuo del modulo). NBA e' off-season a
      // settembre: riattivare "basketball_nba,baseball_mlb" il 1° ottobre
      // (inizio stagione NBA + reset dei 500 crediti mensili).
      SUREBET_SPORTS: "baseball_mlb",
      SUREBET_ODDS_TTL: "21600",
      SUREBET_MIN_REMAINING: "50",
      SUREBET_MIN_MARGIN: "0.005",
      // Hold diagnostico del cron (default 0 = spento): presente su Railway
      // ma non dichiarato → un `config apply` l'avrebbe cancellato. Solo qui,
      // non nel servizio api (privilegio minimo).
      SUREBET_CRON_HOLD_SECONDS: preserve(),
    },
  });

  return project("quotaverace", {
    resources: [api, data, surebet, surebetData],
  });
});