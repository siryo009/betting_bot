import { defineRailway, fn, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const betting_botVolume = volume("betting_bot-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 500 });
  const betting_bot = service("betting_bot", {
    source: github("siryo009/betting_bot", { checkSuites: false }),
    replicas: { "sfo": 1 },
    networking: { privateNetworkEndpoint: "bettingbot" },
    volumeMounts: { "/app/data": betting_botVolume },
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
      ODDS_API_KEY: preserve(),
      ODDS_DAILY_BUDGET: preserve(),
      OPEN_EXPOSURE_CAP_PCT: preserve(),
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
      TENNIS_SANDBOX_ENABLED: preserve(),
      TEST_NOTIFY_KEY: preserve(),
      TOP_DOWN_EV: preserve(),
      TOP_DOWN_MARGIN: preserve(),
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
      ODDS_API_KEY: preserve(),
      QUOTAVERACE_BOT_TOKEN: preserve(),
      SUREBET_BUDGET: "100",
      // Settembre 2026: solo MLB (in stagione). Riattivare
      // "basketball_nba,baseball_mlb" il 1° ottobre (inizio stagione NBA).
      SUREBET_SPORTS: "baseball_mlb",
      SUREBET_ODDS_TTL: "21600",
      SUREBET_MIN_REMAINING: "50",
      SUREBET_MIN_MARGIN: "0.005",
      SUREBET_CRON_HOLD_SECONDS: preserve(),
    },
  });

  return project("creative-vibrancy", {
    resources: [betting_bot, betting_botVolume, surebet, surebetData],
  });
});
