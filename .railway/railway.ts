import { defineRailway, fn, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const betting_botVolume = volume("betting_bot-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 500 });
  const betting_bot = service("betting_bot", {
    source: github("siryo009/betting_bot", { checkSuites: false }),
    replicas: { "sfo": 1 },
    networking: { privateNetworkEndpoint: "bettingbot" },
    volumeMounts: { "/app/data": betting_botVolume },
    env: { ADMIN_CHAT_ID: preserve(), API_FOOTBALL_KEY: preserve(), AUTO_BET_MODE: preserve(), AUTO_BET_STAKE_MODE: preserve(), DECISION_SHADOW_PERSIST: preserve(), ENABLE_LIVE_OU: preserve(), EXA_API_KEY: preserve(), EXECUTION_PROVIDER: preserve(), GOOGLE_API_KEY: preserve(), KELLY_MAX_FRACTION: preserve(), KELLY_MIN_FRACTION: preserve(), ODDS_API_KEY: preserve(), ODDS_DAILY_BUDGET: preserve(), OU_LIVE_MIN_CLOSURES: preserve(), QUOTAVERACE_BOT_TOKEN: preserve(), SETTLEMENT_HEAL_INTERVAL_HOURS: preserve(), STAKE_CAP_HARD: preserve(), STAKE_CAP_PCT: preserve(), STAKE_CAP_PCT_STRONG: preserve(), SX_API_KEY: preserve(), SX_PRIVATE_KEY: preserve(), T60_KILL_WALLET_USDC: preserve(), T60_WINDOW_MIN_MIN: preserve(), TENNIS_SANDBOX_ENABLED: preserve(), TEST_NOTIFY_KEY: preserve() },
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
