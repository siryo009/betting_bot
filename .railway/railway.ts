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
      // Fill rate PER PICK (17/10/2026): un evento per OGNI tentativo di
      // ordine reale (`auto_bet._live_fill`). Serve la misura che i log del
      // container non possono dare (retention di minuti) e che l'exchange non
      // espone (nessuno storico ordini): il giro ripete lo stesso pick ogni
      // 60s, quindi il metrico "riempiti / invii" NON e' la probabilita' di
      // eseguire — lo e' "pick riempiti / pick tentati".
      FILL_RATE_ENABLED: preserve(),
      FILL_RATE_LOG: preserve(),
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
      LIVE_INTEL_FAIL_TTL_H: preserve(),
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
      // (`stake_engine.dynamic_kelly_fraction`) e ticket minimo **1.00 USDC**
      // del MOTORE (= floor dell'EXCHANGE SX). `KELLY_AGGRESSIVE_ENABLED=0`
      // ripristina il percorso storico; `ORDER_FIXED_STAKE_USDC` > 0
      // ripristina l'importo fisso del 28/09. `KELLY_AGGRESSIVE_FRACTION` e'
      // il MASSIMO della banda, `KELLY_AGGRESSIVE_MIN_FRACTION` il minimo.
      //
      // CAP PER-ORDINE (13/10/2026, `auto_bet.enforce_order_ceiling`): e' il
      // tetto che NESSUN layer (Kelly, torneo x1.5, rush) puo' superare, e
      // l'unico parametro che decide la taglia di un ordine reale. Default di
      // CODICE 5%; l'env in produzione e' **12%** (scelta del proprietario del
      // 14/10, che rende raggiungibile il cap dichiarato dal torneo senza
      // superare il recinto). Con l'env presente il default NON ha effetto:
      // `order_ceiling` = equity x questo valore (46.73 -> 5.61 USDC, 4 ordini
      // dentro il recinto 23.36). Se la variabile sparisse il tetto tornerebbe
      // a 2.34 (5%) **in silenzio**: per questo e' dichiarata `preserve()`.
      KELLY_AGGRESSIVE_FRACTION: preserve(),
      KELLY_AGGRESSIVE_MIN_FRACTION: preserve(),
      KELLY_MAX_STAKE_PCT: preserve(),
      KELLY_MIN_TICKET_USDC: preserve(),
      KELLY_AGGRESSIVE_ENABLED: preserve(),
      // CACHE DELLA LETTURA DEL SALDO (17/10/2026, `auto_bet._live_wallet_snapshot`):
      // il punto UNICO che legge il wallet reale. Il 17/10 una raffica di 429 su
      // `/user/balance-v3` (~100 richieste/minuto da ~15 percorsi) ha fatto
      // fallire 309 letture e ha spinto il giro verso la CASSA SIMULATA, cioe'
      // verso una base di misura diversa (la classe del falso stop del 21/09).
      // `LIVE_WALLET_TTL_S` = riuso dell'istantanea entro il ciclo (20s),
      // `LIVE_WALLET_MIN_INTERVAL_S` = intervallo minimo fra due HTTP anche per
      // il percorso d'ordine (6s), `LIVE_WALLET_STALE_MAX_S` = eta' massima
      // dell'istantanea usabile dopo una lettura fallita (120s),
      // `LIVE_WALLET_WARN_TTL_S` = anti-spam dell'avviso (60s).
      LIVE_WALLET_TTL_S: preserve(),
      LIVE_WALLET_MIN_INTERVAL_S: preserve(),
      LIVE_WALLET_STALE_MAX_S: preserve(),
      LIVE_WALLET_WARN_TTL_S: preserve(),
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
      // GATE DI DIPENDENZA / COPULA (14/10/2026, direttiva proprietario: enforce
      // in produzione, moltiplicatore SOLO in [0,1]). Ultimo passo di sizing
      // prima della FASE 3: `COPULA_GUARD` (1 = attivo, 0 = spento),
      // `COPULA_MODE` (enforce), bande di dipendenza `COPULA_MAX_CORR` (0.45) /
      // `COPULA_CRITICAL_CORR` (0.80), peso della coda `COPULA_TAIL_WEIGHT`
      // (0.5) e gradi di liberta' `COPULA_TAIL_NU` (6), budget di CODA
      // `COPULA_DD_LEVEL` (0.35 = perdita di coda ammessa) e
      // `COPULA_DD_QUANTILE` (0.05), cap di cluster per lega
      // `COPULA_CLUSTER_PCT` (0.50 del bankroll), pavimento
      // `COPULA_MIN_MULTIPLIER` (0.25: sotto si blocca invece di ridurre),
      // correlazioni DICHIARATE fra partite diverse (`COPULA_SAME_LEAGUE_RHO`
      // 0.80, `COPULA_SAME_WINDOW_RHO` 0.15, `COPULA_SAME_MATCH_RHO` 0.90),
      // simulazione (`COPULA_SIMS` 2000, `COPULA_SEED` 7).
      COPULA_GUARD: preserve(),
      COPULA_MODE: preserve(),
      COPULA_MAX_CORR: preserve(),
      COPULA_CRITICAL_CORR: preserve(),
      COPULA_TAIL_WEIGHT: preserve(),
      COPULA_TAIL_NU: preserve(),
      COPULA_DD_LEVEL: preserve(),
      COPULA_DD_QUANTILE: preserve(),
      COPULA_MAX_DD_PROB: preserve(),
      COPULA_CLUSTER_PCT: preserve(),
      COPULA_MIN_MULTIPLIER: preserve(),
      COPULA_SAME_LEAGUE_RHO: preserve(),
      COPULA_SAME_WINDOW_RHO: preserve(),
      COPULA_SAME_MATCH_RHO: preserve(),
      COPULA_WINDOW_MIN: preserve(),
      COPULA_SIMS: preserve(),
      COPULA_SEED: preserve(),
      COPULA_VIG_HAIRCUT: preserve(),
      // M6 FASE 3+4 — RISK PARITY + DRAWDOWN REBALANCING (16/10/2026): la
      // promozione dei due moduli di rischio di PORTAFOGLIO nel percorso
      // ordini. `PORTFOLIO_SIZING` e' l'UNICO interruttore della promozione:
      // `off` (nessuna valutazione, nessun import, nessuna lettura del ledger),
      // `shadow` (default: i due gate misurano e loggano cosa farebbero, gli
      // stake NON cambiano) oppure `enforce` (riducono davvero e scartano
      // fail-closed chi scende sotto il ticket minimo del motore). Gira dopo
      // il tetto per-ordine e prima della copula.
      // Risk parity (`risk_parity.py`): `RISK_PARITY_GUARD` (1 = attivo),
      // `RISK_PARITY_MODE`, `RISK_PARITY_MAX_SHARE_RATIO` (quota massima di un
      // bucket), `RISK_PARITY_MIN_MULTIPLIER` (pavimento: sotto si blocca
      // invece di ridurre), campione `RISK_PARITY_MIN_SAMPLE` /
      // `RISK_PARITY_DAYS` / `RISK_PARITY_MIN_DAYS`, griglia `RISK_PARITY_GRID`,
      // cache `RISK_PARITY_TTL_S`.
      // Drawdown (`drawdown_rebalancer.py`): `DRAWDOWN_REBALANCE` (1 = attivo),
      // `DRAWDOWN_REBALANCE_MODE`, `DRAWDOWN_REBALANCE_TIERS` (curva drawdown ->
      // moltiplicatore), `DRAWDOWN_REBALANCE_MIN_MULTIPLIER`,
      // `DRAWDOWN_REBALANCE_MIN_TICKET_USDC`, `DRAWDOWN_REBALANCE_TTL_S`.
      PORTFOLIO_SIZING: preserve(),
      RISK_PARITY_GUARD: preserve(),
      RISK_PARITY_MODE: preserve(),
      RISK_PARITY_MAX_SHARE_RATIO: preserve(),
      RISK_PARITY_MIN_MULTIPLIER: preserve(),
      RISK_PARITY_MIN_SAMPLE: preserve(),
      RISK_PARITY_DAYS: preserve(),
      RISK_PARITY_MIN_DAYS: preserve(),
      RISK_PARITY_GRID: preserve(),
      RISK_PARITY_TTL_S: preserve(),
      DRAWDOWN_REBALANCE: preserve(),
      DRAWDOWN_REBALANCE_MODE: preserve(),
      DRAWDOWN_REBALANCE_TIERS: preserve(),
      DRAWDOWN_REBALANCE_MIN_MULTIPLIER: preserve(),
      DRAWDOWN_REBALANCE_MIN_TICKET_USDC: preserve(),
      DRAWDOWN_REBALANCE_TTL_S: preserve(),
      // DIXON-COLES (16/10/2026, `dixon_coles.py`): stima di `rho` con
      // time-decay sui `match_results` reali + promozione nel motore.
      // `DIXON_COLES_ENABLED` spegne il job, `DIXON_COLES_APPLY` accende
      // l'applicazione al modello (default OFF: accenderla cambia le
      // probabilita' di TUTTE le corsie, quindi e' una decisione esplicita).
      // `DIXON_COLES_STATE` e' il file sul volume dove vive la stima.
      DIXON_COLES_ENABLED: preserve(),
      DIXON_COLES_APPLY: preserve(),
      DIXON_COLES_STATE: preserve(),
      DIXON_COLES_DAYS: preserve(),
      DIXON_COLES_MAX_MATCHES: preserve(),
      DIXON_COLES_MAX_GOALS: preserve(),
      DIXON_COLES_RHO_MIN: preserve(),
      DIXON_COLES_RHO_MAX: preserve(),
      DIXON_COLES_RHO_STEP: preserve(),
      DIXON_COLES_REFINE_STEP: preserve(),
      DIXON_COLES_SAFE_MIN: preserve(),
      DIXON_COLES_SAFE_MAX: preserve(),
      DIXON_COLES_HALF_LIFE_DAYS: preserve(),
      // GATE DI CALIBRAZIONE (16/10/2026): il rho stimato si applica solo se
      // il Brier 1X2 FUORI CAMPIONE non peggiora. Sul ledger reale il valore
      // che massimizza la verosimiglianza (-0.30) peggiora la calibrazione:
      // senza questo gate la "stima migliore" sarebbe stata un danno.
      DIXON_COLES_CALIB_GATE: preserve(),
      DIXON_COLES_CALIB_MIN_GAIN: preserve(),
      DIXON_COLES_HOLDOUT_SHARE: preserve(),
      DIXON_COLES_MIN_HOLDOUT: preserve(),
      ORDER_FIXED_STAKE_USDC: preserve(),
      ORDER_MAX_STAKE_USDC: preserve(),
      T60_MAX_STAKE_USDC: preserve(),
      // SOGLIA MORBIDA del CB2 (21/10/2026): con l'equity che si avvicina al
      // muro patrimoniale (10 USDC) si smette di APRIRE nuove puntate
      // lasciando respirare i settlement aperti, invece di arrivare al muro
      // duro e restare fermi finche' un admin non fa `/t60reset`. 0 = spenta.
      CB2_SOFT_FLOOR_USDC: preserve(),
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
      // Ghigliottina PRE-MATCH / hard pruning (08/10/2026): oltre N ore dal
      // kickoff un pick esce dal board prima di qualunque valutazione. E' la
      // regola INDIPENDENTE dalla finestra (`T60_EXECUTION_ONLY=0` non la
      // spegne), nata dal pick Botafogo RJ-CR Vasco da Gama ancora visibile
      // 22h dopo il kickoff.
      PREMATCH_MAX_AGE_H: preserve(),
      OU_LIVE_MIN_CLOSURES: preserve(),
      // Gate EV dei MERCATI LIQUIDI (04/10/2026, punto 4): AH/OU/Totals/BTTS/ML
      // usano una soglia dedicata (default 1.0%) mentre 1X2/eSports/tennis
      // restano a `value_filter.EV_MIN`. `EV_LIQUID_MARKETS` e' il CSV dei
      // mercati a cui applicarla (una sola definizione, in `value_filter`).
      EV_MIN_LIQUID: preserve(),
      EV_LIQUID_MARKETS: preserve(),
      // Soglia EV del TIER di lega (08/10/2026, `value_filter.ev_min`):
      // dimensione AGGIUNTIVA a quella per mercato, con precedenza "la piu'
      // severa" (`max`) — il tier puo' solo stringere, mai allargare.
      // `EV_MIN_CORE` = leghe Tier-1/Core, `EV_MIN_OTHER` = tutte le altre.
      EV_MIN_CORE: preserve(),
      EV_MIN_OTHER: preserve(),
      // Closing line a T-0 (04/10/2026, punto 5): la routine `closing_line.py`
      // scrive `clv_history.closing_odds` DENTRO la finestra T-PRE..T+POST
      // rispetto al kickoff (default 10' prima / 5' dopo). Sola lettura dalle
      // cache sharp: zero crediti, zero ordini.
      CLOSING_LINE_ENABLED: preserve(),
      CLOSING_LINE_PRE_MIN: preserve(),
      CLOSING_LINE_POST_MIN: preserve(),
      CLOSING_LINE_MAX_ROWS: preserve(),
      // Harness ML (15/10/2026, `ml_harness.py`): walk-forward cronologico che
      // confronta boosting/logistica/prior sulle feature storiche di
      // `match_features`. Sola misura (zero ordini, zero crediti): queste env
      // esistono perche' un `config apply` non deve distruggere la taratura.
      MLH_FOLDS: preserve(),
      MLH_MIN_TRAIN: preserve(),
      MLH_SEED: preserve(),
      // Rating bayesiani (15/10/2026, `bayes_ratings.py`): inferenza gerarchica
      // con shrinkage empirico-Bayes + walk-forward cronologico. Anche questo
      // e' SOLO MISURA (zero ordini, zero crediti): le env esistono perche' un
      // `config apply` non deve distruggere il campione di validazione ne' la
      // taratura del prior, e perche' `BAYES_STATE` decide DOVE vive il fit
      // (volume, non filesystem effimero del container).
      BAYES_ITERATIONS: preserve(),
      BAYES_LR: preserve(),
      BAYES_MIN_MATCHES: preserve(),
      BAYES_FOLDS: preserve(),
      BAYES_MIN_FOLD: preserve(),
      BAYES_SIGMA: preserve(),
      BAYES_SIGMA_FLOOR: preserve(),
      BAYES_SIGMA_CEIL: preserve(),
      BAYES_EB_SEED: preserve(),
      BAYES_DAYS: preserve(),
      BAYES_MAX_MATCHES: preserve(),
      BAYES_STATE: preserve(),
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
      // Riconciliazione del picco settimanale (09/10/2026): una bet con payout
      // GIA' accreditato da SX (mercato risolto) ma ancora aperta nel ledger
      // fino al job di settlement gonfiava l'equity campionata (available +
      // stake aperto) e faceva scattare il circuit breaker settimanale su un
      // -15.4% inesistente. `reconciled_equity()` corregge i campioni spiegati
      // dal P/L delle righe saldate entro questa tolleranza (USDC).
      WEEKLY_RECONCILE_TOLERANCE_USDC: preserve(),
      STAKE_CAP_HARD: preserve(),
      STAKE_CAP_PCT: preserve(),
      STAKE_CAP_PCT_STRONG: preserve(),
      SX_API_KEY: preserve(),
      SX_BOOK_LEVELS_KEPT: preserve(),
      SX_PRIVATE_KEY: preserve(),
      T60_KILL_WALLET_USDC: preserve(),
      // STOP-LOSS giornaliero/settimanale e TETTO ASSOLUTO per-ordine
      // (21/10/2026). Con equity ~21 USDC e tetto dinamico 12% (2,53) un
      // singolo ordine perso vale ~12% del capitale: le soglie 5%/12% si
      // armavano al PRIMO ordine perso e fermavano le puntate 24h (misurato:
      // nessun ordine dal 18/10 04:01 al 20/10 22:53). Le soglie larghe ~2
      // (daily) e ~8 (weekly) ordini persi fanno misurare una SERIE, non un
      // colpo; `ORDER_MAX_STAKE_USDC` fissa il rischio per ordine a 1 USDC
      // finche' il capitale non torna sopra ~50.
      DAILY_STOP_LOSS_PCT: preserve(),
      DAILY_STOP_HOURS: preserve(),
      WEEKLY_STOP_LOSS_PCT: preserve(),
      WEEKLY_STOP_HOURS: preserve(),
      WEEKLY_STOP_WINDOW_H: preserve(),
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
      // Tornei NUOVI (11/10/2026): il job a 6h non basta per un torneo attivato
      // a meta' giornata (WTA Wuhan: 4 partite in finestra senza oracolo =
      // zero ordini). Il controllo delle chiavi e' GRATUITO, quindi gira ogni
      // `TENNIS_ORACLE_CHECK_MIN` (10') e paga 1 credito solo per un torneo mai
      // prezzato; `TENNIS_NEW_KEYS_CHECK_S` e' la memo dell'elenco chiavi.
      TENNIS_ORACLE_CHECK_MIN: preserve(),
      TENNIS_NEW_KEYS_CHECK_S: preserve(),
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
      // Catena di Markov tennis (15/10/2026, dentro `tennis_quant`): la
      // probabilita' punto-per-punto e la CALIBRAZIONE delle tenute di
      // servizio dai game reali del referto (`match_results.detail_*`). Anche
      // qui solo misura: gli interruttori esistono per tornare al fallback
      // Poisson-da-hold se la calibrazione non regge.
      TENNIS_MARKOV_ENABLED: preserve(),
      TENNIS_MARKOV_BEST_OF: preserve(),
      TENNIS_MARKOV_TIEBREAK: preserve(),
      TENNIS_MARKOV_TIEBREAK_TO: preserve(),
      TENNIS_MARKOV_SURFACE_DELTA: preserve(),
      TENNIS_MARKOV_CALIBRATE: preserve(),
      // Corsia FOOTBALL USA (11/10/2026, `gridiron_lane.py`): discovery SX
      // sportId 8 (NFL/NCAA) + oracolo Pinnacle `regions=eu` a 2 esiti + gate
      // EV e fascia quota 1.30-3.00. Gli ORDINI sono nella corsia LIVE di
      // `auto_bet` (`_gridiron_picks`); il job aggiorna l'oracolo (1 credito
      // per lega con cache scaduta) e registra la telemetria.
      GRIDIRON_LANE: preserve(),
      GRIDIRON_SX_SPORT_ID: preserve(),
      GRIDIRON_TYPES: preserve(),
      GRIDIRON_SPORTS: preserve(),
      GRIDIRON_HOURS_AHEAD: preserve(),
      GRIDIRON_MAX_MARKETS: preserve(),
      GRIDIRON_EV_MIN: preserve(),
      GRIDIRON_ODDS_MIN: preserve(),
      GRIDIRON_ODDS_MAX: preserve(),
      GRIDIRON_MIN_INV_SUM: preserve(),
      GRIDIRON_MAX_INV_SUM: preserve(),
      GRIDIRON_ORACLE_TTL_MIN: preserve(),
      GRIDIRON_REQ_BUDGET_DAY: preserve(),
      // Tetto di SPESA in crediti (non richieste): coi tipi a linea
      // (342/28) una fetch costa 3 crediti invece di 1, quindi il solo
      // GRIDIRON_REQ_BUDGET_DAY porterebbe il tetto da 8 a 24 cr/giorno.
      GRIDIRON_CREDIT_BUDGET_DAY: preserve(),
      GRIDIRON_ORACLE_CACHE: preserve(),
      GRIDIRON_LANE_STATE: preserve(),
      GRIDIRON_DISCOVERY_TTL_S: preserve(),
      GRIDIRON_JOB_INTERVAL_MIN: preserve(),
      GRIDIRON_ODDS_BASE: preserve(),
      // TORNEO RETURN-BASED (11/10/2026, `tournament.py`): layer di staking
      // SOPRA il percorso live (SX "Gridiron Gauntlet": i punti sono i ritorni
      // lordi delle vincite, le perdite non tolgono nulla). Puo' solo ALZARE
      // lo stake di un segnale GIA' approvato, entro un budget di esposizione,
      // un cap per ordine e una soglia di sicurezza sull'equity. `TOURNAMENT_MODE`
      // e' SPENTO di default: con 0 il giro e' identico a prima.
      TOURNAMENT_MODE: preserve(),
      TOURNAMENT_LABEL: preserve(),
      TOURNAMENT_BUDGET_USDC: preserve(),
      TOURNAMENT_SAFETY_FLOOR_USDC: preserve(),
      TOURNAMENT_MAX_STAKE_USDC: preserve(),
      TOURNAMENT_AGGRESSION: preserve(),
      TOURNAMENT_CLASSES: preserve(),
      // Apertura delle leghe fuori whitelist alla SOLA corsia torneo
      // (12/10/2026, `value_filter.tournament_all_leagues()`): con la doppia
      // chiave `TOURNAMENT_MODE=1` + `TOURNAMENT_OPEN_LEAGUES=1` le leghe
      // `blocked` tornano giocabili (fallback severo di strategia) perche' nel
      // formato RETURN le perdite non tolgono punti. Spegnerla e' una sola
      // variabile: senza la seconda chiave il gate di lega resta com'e'.
      TOURNAMENT_OPEN_LEAGUES: preserve(),
      // RUSH FINALE del torneo (13/10/2026, `rush_final.py`): finestra
      // 17-20/10 in cui il sizing viene accelerato e il capitale RICIRCOLATO
      // in fretta (TTL degli ordini RESTING accorciato + ritiro degli ordini
      // che non trovano controparte). TUTTE lette a RUNTIME: si accende,
      // si spegne e si tara senza redeploy — e il 21/10 il modulo si spegne
      // da solo perche' la finestra e' passata (`RUSH_FINAL` ON di default,
      // INERTE fuori finestra). Il tetto resta `min(cap rush, cap torneo,
      // budget residuo, spazio del recinto)`: un `config apply` non deve
      // distruggere ne' la finestra ne' i cap decisi a mano.
      RUSH_FINAL: preserve(),
      RUSH_START: preserve(),
      RUSH_END: preserve(),
      RUSH_BUDGET_USDC: preserve(),
      RUSH_MAX_STAKE_USDC: preserve(),
      RUSH_AGGRESSION: preserve(),
      RUSH_CLASSES: preserve(),
      RUSH_RECYCLE_WINDOW_MIN: preserve(),
      RUSH_RECYCLE_BOOST: preserve(),
      RUSH_RESTING_TTL_MIN: preserve(),
      RUSH_CLEAR_RESTING: preserve(),
      RUSH_CLEAR_RESTING_AFTER_MIN: preserve(),
      // TRACKER AUTOMATICO del CLV (13/10/2026, `clv_tracker.py`): misura la
      // quota d'ACQUISTO dei trade contro la closing line (Pinnacle / SX Bet).
      // `clv_tracker_job` gira ogni 6h (sola lettura del ledger, zero
      // crediti) e `CLV_SX_CAPTURE` accende la cattura della chiusura SX a
      // T-0 dentro `closing_line_job`. La FINESTRA di cattura resta quella di
      // `closing_line` (T-10..T+5): le env qui sotto possono solo stringerla.
      CLV_TRACKER_ENABLED: preserve(),
      CLV_SX_CAPTURE: preserve(),
      CLV_SX_PRE_MIN: preserve(),
      CLV_SX_POST_MIN: preserve(),
      // CLASSIFICA del torneo nel bot (12/10/2026,
      // `tournament_leaderboard.py`): "tieni aggiornato l'andamento della
      // classifica". Lettura PUBBLICA e GRATUITA (zero crediti the-odds-api,
      // zero chiavi, zero ordini) da `api.sx.bet`, che pero' SCARTA le
      // richieste senza `User-Agent` (da qui l'env dedicata).
      // `TOURNAMENT_USERNAME` e' il nome in classifica, `TOURNAMENT_TOP_RANKS`
      // l'ultima posizione premiata (30) e `TOURNAMENT_BOARD_NOTIFY_HOURS`
      // l'anti-spam delle notifiche (12h = 2 avvisi/giorno).
      TOURNAMENT_BOARD: preserve(),
      TOURNAMENT_BOARD_INTERVAL_MIN: preserve(),
      TOURNAMENT_BOARD_NOTIFY_HOURS: preserve(),
      TOURNAMENT_BOARD_TTL_S: preserve(),
      TOURNAMENT_BOARD_TIMEOUT_S: preserve(),
      TOURNAMENT_USERNAME: preserve(),
      TOURNAMENT_TOP_RANKS: preserve(),
      SX_TOURNAMENT_API_BASE: preserve(),
      SX_TOURNAMENT_UA: preserve(),
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
      // TIER ammessi al refetch a PAGAMENTO (07/10/2026, `ORACLE_PAID_TIERS`):
      // CSV di `core`/`probation`, default `core` (comportamento storico).
      // NON cambia la SPESA (budget e tetto per lega restano gli stessi),
      // cambia l'ALLOCAZIONE delle unita' gia' pagate: con `core,probation`
      // una lega in probation in finestra puo' ricevere un fetch. Da qui la
      // necessita' di preserve(): un `config apply` non deve spegnere (o
      // accendere) la spesa sulle probation in silenzio.
      ORACLE_PAID_TIERS: preserve(),
      // Tetto di CONCENTRAZIONE per lega (06/10/2026). Il tetto giornaliero
      // conta CHIAMATE, non leghe: misurato in produzione due fetch sulla
      // stessa lega (UEFA Nations League) a 5 minuti di distanza hanno
      // consumato ENTRAMBE le unita' di `ORACLE_BUDGET_DAY=2`, lasciando senza
      // oracolo le altre leghe Core con pick in finestra (AFCON: due pick
      // pronti e mai prezzati). Default 1: con budget 2 le unita' vanno a due
      // leghe distinte. Alzarlo solo insieme al budget (3 crediti per unita').
      ORACLE_MAX_CALLS_PER_LEAGUE: preserve(),
      // Stato del budget GIORNALIERO dell'oracolo (06/10/2026). Il contatore
      // era solo in-process: ogni riavvio (deploy, restart della piattaforma)
      // riapriva il tetto e il 05/10 sono state pagate 14 leghe con
      // `ORACLE_BUDGET_DAY=2` (42 crediti su 6 di tetto). Ora e' persistito sul
      // volume e seminato alla prima fetch del processo.
      ORACLE_BUDGET_STATE: preserve(),
      // Finestra di fetch dell'oracolo a linea (minuti, default 120 dal
      // 05/10/2026, era 70): si ordina solo nella finestra esecutiva
      // T-180..T-2, quindi non si scarica piu' l'intero palinsesto della lega.
      // 120' e' anche cio' che rende pagabile il primo checkpoint di refetch
      // (T-120'). Sostituisce ORACLE_PICK_WINDOW_H (orizzonte a ore, rimosso
      // il 03/10/2026).
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
      // Stato PERSISTENTE dei checkpoint di refetch (05/10/2026): per ogni
      // partita ricorda quale tentativo (T-120'/T-70') e' gia' stato onorato,
      // cosi' un mercato che Pinnacle non pubblica non viene richiesto a ogni
      // ciclo di 60s (fino a 1440 richieste/giorno sulla stessa partita).
      ORACLE_CHECKPOINT_STATE: preserve(),
      // ORACOLO h2h ON-DEMAND (11/10/2026, `odds_api.fetch_h2h_odds` +
      // `line_oracle.fetch_h2h_for_pick`): il ramo 1X2 pagava il fail-closed
      // perche' la cache della lega era SCADUTA — rotazione a 7 giorni contro
      // un tetto di freschezza di 24h significa ~5 giorni su 7 di oracolo
      // mancante (misurato il 10/10: Brasileirao 62,5h, Superliga Danimarca
      // 46,5h). Qui si paga la SOLA `h2h` (1 credito, non 3) sulla cache di
      // ROTAZIONE `toa_<sport>.json`, la stessa che `load_oracle` legge.
      // Budget GIORNALIERO DEDICATO, separato da `ORACLE_BUDGET_DAY` (tarato su
      // chiamate da 3 crediti per i mercati a linea): default di codice 2
      // chiamate/giorno = 2 crediti. `0` spegne il percorso h2h.
      ORACLE_H2H_BUDGET_DAY: preserve(),
      // Tetto di concentrazione per lega del percorso h2h (come il gemello a
      // linea): con budget 2 le unita' vanno a due leghe distinte, non due
      // volte sulla stessa.
      ORACLE_H2H_MAX_CALLS_PER_LEAGUE: preserve(),
      // Stato PERSISTITO del budget h2h: file SEPARATO da quello a linea, cosi'
      // il reset o il tetto di un percorso non azzera/consuma l'altro. Senza
      // preserve() un `config apply` riporterebbe il contatore al path di
      // default, riaprendo il tetto dopo ogni deploy.
      ORACLE_H2H_BUDGET_STATE: preserve(),
      // Dedup per lega del fetch h2h on-demand (secondi, default 120): il gate
      // gira su TUTTO il board e piu' pick della stessa lega non devono pagare
      // la stessa chiamata due volte.
      ORACLE_H2H_DEDUP_S: preserve(),
      // Pre-filtro SX del candidato PRIMA di qualunque spesa oracolo
      // (05/10/2026, `auto_bet._sx_prefilter`): quota fuori dalla fascia
      // giocabile o profondita' SX insufficiente sulla leg giocata -> il pick
      // non e' ordinabile, quindi non si paga l'EV per conoscerlo. Soglia in
      // USDC (default 20.0).
      SX_PREFILTER_MIN_DEPTH_USDC: preserve(),
      // ORDINI RESTING GTC su SX Bet (10/10/2026, `resting_orders.py`): il
      // taker IOC viene CANCELLED senza controparte (119/126 il 25/09-02/10)
      // perche' SX e' un exchange senza market maker. Un ordine RESTING resta
      // sul book e attende il flusso, con la PROPRIETA' della ladder che rende
      // l'EV sicuro: `decimal_to_pct_scaled` arrotonda la probabilita' PER
      // DIFETTO, quindi il fill avviene alla quota del segnale o MEGLIO.
      // La job (ogni 5') riconcilia i fill e cancella prima del kickoff.
      // Dichiarate con preserve(): un `config apply` non deve distruggere
      // interruttore/tetti/TTL che l'operatore tara senza redeploy.
      RESTING_ORDERS: preserve(),
      RESTING_MAX_OPEN: preserve(),
      RESTING_TTL_MIN: preserve(),
      RESTING_CANCEL_BEFORE_MIN: preserve(),
      RESTING_EXPIRY_MARGIN_S: preserve(),
      RESTING_STATE: preserve(),
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
