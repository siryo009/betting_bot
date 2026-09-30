# AGENTS.md — Memoria di progetto (QuotaVerace)

> File di memoria per l'agente AI. Rileggere **all'inizio di ogni sessione** su
> questo progetto per ripartire senza perdere contesto. I documenti di
> riferimento sono: `DEPLOY.md` (infrastruttura), `STRATEGY.md` (strategia di
> gioco — **la priorità assoluta del progetto**), `README.md`.

## Cos'è QuotaVerace

Sistema di **value betting e surebet sul calcio**: motore di probabilità
(Poisson + Dixon-Coles con rating time-decay), confronto con le quote reali
dei bookmaker (odds API), filtro value (+EV), scanner arbitraggio, bot
Telegram, sito web e backtest. Obiettivo dichiarato del proprietario: **fare
profitto sulle scommesse** — ogni lavoro deve servire a migliorare l'edge.

## Architettura in 30 secondi

```
run_all.py          → avvia web_api.py (thread) + bot.py (long-polling, main)
bot.py              → bot Telegram (comandi, job schedulati, segnali)
web_api.py          → API JSON (senza framework, threading.HTTPServer)
tracker.py          → DB SQLite (schema + helper): segnali, analisi, cassa, ratings
fixture_engine.py   → analisi partite: modello vs mercato (CUORE STRATEGICO)
market_calib.py     → devigging + blend dinamico + CLV vig-free + longshot bias
ml_ensemble.py      → ensemble Poisson + Logistic Regression (numpy-only)
probability_calibration.py → calibrazione isotonica PAVA numpy-only dell'ensemble
line_movement.py    → price snapshots, RLM detection, steam moves
bookmaker_advantage.py → soft book lag detection vs Pinnacle
adaptive_staking.py → Kelly frazionato dinamico + drawdown protection
secure_logging.py   → filtro log: maschera segreti/token, httpx silenzioso
rating_engine.py    → rating squadre time-decay (shrink usa COUNT reale `n`, NON `wsum`!)
team_names.py       → risoluzione nomi squadra bookmaker→DB (11/09: 'Tottenham
                      Hotspur'→'Tottenham', 'Wrexham'→'Wrexham AFC', ...)
poisson_engine.py   → modello Poisson/Dixon-Coles
value_filter.py     → gate EV + mercato, is_sane()
backtest.py         → calibrazione EV vs ROI, split "batte il mercato"
market_diagnose.py  → diagnosi calibrazione per mercato (ROI vs EV)
odds_ingest.py      → ingestione quote da odds API (cache in data/)
odds_api.py         → client the-odds-api (rate limit, quota giornaliera) — quote E CLV
football_hist.py    → storico risultati (2022-2024, piano free API-Football) per le ratings
surebet_scanner.py  → prototipo scanner arbitraggi (solo mock, NON in produzione)
surebet_engine.py   → scanner arbitraggi INDIPENDENTE (05/09): h2h a 2 esiti
                      NBA/MLB/Tennis, soft vs sharp via the-odds-api, cache e
                      log propri (data/surebet/), mai import da tracker/bot
                      (loop separato: venv/bin/python surebet_engine.py --loop N)
tennis_sandbox.py   → SANDBOX tennis (08/09, ELO esteso il 09/09): paper
                      trading Moneyline 2 vie su SX Bet (type 52), baseline
                      ELO superficie-specifico (cemento/terra/erba) con
                      time-decay 30/60gg seminata dal mercato, +EV con
                      anti-spurio (inv_sum 0.98-1.08), ledger SQLite dedicato
                      (data/tennis_sandbox/), ZERO ordini reali e zero crediti
                      the-odds-api (letture SX pubbliche)
data/               → cache JSON + DB sqlite + modello ensemble
backtest_mc.py      → backtest walk-forward ensemble+Kelly con Monte Carlo (ROI, MaxDD)
backup_manager.py   → backup DB+dataset ML (integrity check, rotazione, /backup)
execution_engine.py → ESECUZIONE ordini via provider (SX Bet V3 crypto / Smarkets /
                      BetInAsia BLACK / MollyBet): provider interface + DryRun +
                      probe a stake minimo (1€) latenza/slippage
webapp/             → Next.js (Vercel): dashboard, cassa, schedina, calendario, backtest, value...
```

**Il tripwire Betfair è stato RIMOSSO il 06/09**: i moduli `betfair_client.py`,
`daily_scanner.py`, `daily_scan_job.py`, `surebet_pipeline.py` non esistono più,
ma la direzione è cambiata — l'ESECUZIONE passa ora da un provider di
`execution_engine.py` (**SX Bet** V3 crypto dal 07/09, **Smarkets** dal
07/09, o gli aggregatori BetInAsia BLACK / MollyBet Betfair-compatible).
Refertazione = the-odds-api (`odds_api.fetch_scores`,
risultati stagione corrente — la stessa chiave delle quote); quote/CLV =
the-odds-api. API-Football serve SOLO allo storico ratings 2022-2024
(`football_hist.py`): il piano free NON copre la stagione corrente (verificato
04/09), quindi non può saldare le partite del 2026. auto_bet: SIM di
DEFAULT, ordini LIVE via `execution_engine` dal 08/09 con
`AUTO_BET_MODE=live` + provider reale configurato (EXECUTION_PROVIDER +
credenziali; oggi SX Bet). Il vincolo "settlement = the-odds-api" resta garantito da
`test_settlement_source.py`; il vincolo "nessuna credenziale in chiaro" da
`test_secret_hygiene.py`.

**Backend e bot stanno nello STESSO container Railway** (volume unico su
`/app/data`). Niente servizi separati con volumi divisi.

## Deploy (dettagli in DEPLOY.md)

| Cosa | Dove | Come |
|---|---|---|
| Backend (bot + API) | **Railway** | `git push` su main → auto-deploy (Dockerfile → `run_all.py`) |
| Volume persistente | Railway `api-volume` → `/app/data` | DB, cache, log, backup sopravvivono ai redeploy |
| Frontend webapp | **Vercel** | da `webapp/`: `vercel --prod` |
| Infrastruttura Railway | IaC in `.railway/railway.ts` | `railway config apply --yes --confirm-destructive` (⚠️ il CLI `railway` vive in `~/.npm-global`, NON cancellarlo nelle pulizie) |

- API base di produzione: `https://api-production-dffd.up.railway.app`
- Sito: `https://quotaverace.vercel.app`
- Shell sul container: `railway ssh --service api` (chiave SSH locale
  `~/.ssh/id_ed25519` registrata come "quotaverace-debug"; nel container NON
  c'è la CLI `sqlite3` → interrogare il DB con `python3 -c "..."`).
- Variabili chiave: `QUOTAVERACE_BOT_TOKEN`, `API_FOOTBALL_KEY`, `RAILWAY_TOKEN`,
  `NEXT_PUBLIC_API_BASE` (nel progetto Vercel `quotaverace`).

## Comandi utili

```bash
venv/bin/python -m pytest -q          # test (481+, ~5 min)
venv/bin/python -c "..."              # script rapidi (usare venv/bin/python, NON python)
cd webapp && npm run build            # build Next.js
```

## Convenzioni e regole d'oro

1. **La strategia è l'elemento più importante** — prima di ogni feature, chiedersi
   se migliora l'edge. Ricerca web periodica su strategie 2026 (CLV, devigging,
   favourite-longshot bias).
2. **Il modello è corretto ORA** (fix 31/08): rating con shrink su `n` reale
   (il bug `wsum` collassava tutte le squadre a 1.0). Non "sistemare" la formula
   senza capire questo.
3. **Edge = battere il mercato devigato** (min +2pp value, +4pp strong_value
   dal 21/09/2026 — era +3pp/+5pp), EV sulla probabilità blend
   modello+mercato. Vedere `STRATEGY.md`.
4. **Mai cancellare `~/.npm-global`** (contiene CLI railway e vercel).
5. **Python**: usare sempre `venv/bin/python`, mai `python` nudo.
6. **DB**: migrazioni schema con ALTER TABLE idempotente in `tracker.py`
   (già fatto per market_prob/market_edge) — verificare sul volume dopo il deploy.
7. **Token**: rotazioni completate e verificate il **01/09**, il **02/09** e
   il **04/09** (Telegram attivo su `api`/`production`, `getMe` 200).
   ⚠️ Il token del 02/09 (incollato in chat) è stato REVOCATO il 04/09 via
   Opzione A: il vecchio risponde 401, il nuovo è attivo e nel vault cifrato
   locale. Regola permanente: qualunque token finito in un canale pubblico
   va considerato compromesso e rotato subito, SENZA incollarlo in chat
   (Opzione A: utente genera da @BotFather e fa `railway variables --service
   api --set QUOTAVERACE_BOT_TOKEN=...`; l'agente verifica e fa il merge nel
   vault senza mai vedere il valore — GitHub: PAT fine-grained → vault;
   Telegram: @BotFather). Vincolo custodito dal tripwire
   `test_secret_hygiene.py` (rompe se compare una credenziale in chiaro).
8. **Push su `main` = deploy IMMEDIATO → prima di pushare controllare i
   marker di conflitto**: `grep -rn "^<<<<<<<" *.py` deve essere vuoto. Il
   13/09 un conflitto di merge committato in `auto_bet.py` ha messo giù
   l'INTERA produzione (502, bot fermo, zero settlement) e nessuno se ne e'
   accorto per ore. I test vanno eseguiti PRIMA del push.

## Segreti: vault cifrato (`secrets/`)

- Tutti i segreti locali vivono in `secrets/vault.bin` (Fernet + PBKDF2,
  `SECRETS_MASTER_KEY` nel `.env` gitignored, chmod 600). Mai plaintext nel
  repo, mai loggati, caricati solo in memoria da `secrets_store.py` al
  bootstrap (`config.py` → `load_secrets_dir`).
- **Rotazione chiave (12/09/2026)**: `SECRETS_MASTER_KEY` ruotata (la vecchia
  era esposta in chat) — nuovi segreti cifrati con chiave fresca.
  Vecchia `secrets/betfair/` (cert SSL) rimossa.
- CLI: `venv/bin/python secrets_store.py vault|check|get NOME`. Per aggiungere
  un segreto: file plaintext in `secrets/` → `vault --commit` (cancella il
  plaintext). Se perdi `SECRETS_MASTER_KEY` senza plaintext, i segreti sono
  persi.
- Su Railway i segreti restano nelle env vars del progetto (cassaforte vera).
- `vault --commit` NON è ricorsivo: considera solo i file
  diretti in `secrets/` (`iterdir`). MAI mettere `*.key`/`*.pem`
  direttamente in `secrets/`. (Cartella `secrets/betfair/`
  rimossa il 12/09.)
- **Tripwire igiene segreti** (`test_secret_hygiene.py`): la suite ROMPE se un
  sorgente .py contiene una credenziale in chiaro (formati noti: token
  Telegram, GitHub PAT, Google API key, AWS, Slack, Stripe, PEM, Bearer;
  assegnazioni a nomi credential-like; URL user:password) o se `.env*`/
  `secrets/` finiscono tracciati in git. Tutte le chiavi si leggono SOLO da
  env (`.env` gitignored + vault): audit completato con zero valori hardcoded.
  Rimossa da `test_secure_logging.py` una credenziale Telegram REALE ma
  rotata (era finita lì come esempio nel fix del 01/09): lo scrub maschera
  sul FORMATO, quindi ora il test usa un fake marcato `fake/test`.

## Push automatico (credenziali GitHub)

- Il deploy è automatico: Railway ridistribuisce da solo a ogni push su `main`.
- Il push lo fa l'agente a fine lavoro con `GIT_ASKPASS=$(pwd)/.askpass_github.sh`
  (script gitignored che apre il VAULT e passa `GITHUB_TOKEN` a git senza mai
  stamparlo: env | .env → chiave maestra → `secrets_store.get_secret`).
- Il token va rinnovato quando scade o dopo l'esposizione in chat (flusso:
  fine-grained PAT → Contents RW → va nel VAULT, non più nel `.env`).

## Stato attuale (aggiornato al 12/09/2026)

- **SETTLEMENT NATIVO SX (12/09, deploy `0305ffc`, VERIFICATO IN PRODUZIONE)**:
  le leghe che SX scansiona ma the-odds-api NON copre (Primera A Colombia,
  Primera Nacional, K2-League) lasciavano bet/previsioni aperte per sempre —
  nessuna fonte esterna puo' refertarle. Ora `_results_from_sx` (sx_signals.py)
  legge l'esito DALL'EXCHANGE, gratis e senza matching per nome:
  1) `markets/find` sui market_hash salvati sulle bet (batch da SX_FIND_BATCH=30,
     docs: max 30 hash/chiamata): ogni mercato binario porta SEMPRE i punteggi
     dell'evento (teamOneScore/teamTwoScore) e, se saldato, `outcome` — la
     semantica e' relativa alla GAMBA ("T1 vs Not T1": outcome 1 = vince T1),
     quindi il verdetto 1X2 lo emette SEMPRE settle_bets/settle_predictions dai
     punteggi (fail-closed, mai dal campo outcome della gamba). Si saldano
     anche le bet ORFANE senza riga in `matches` (i nomi vengono dalla find;
     la find riporta anche la lega vera: America MG–Nautico era Serie B).
  2) `/markets/active` per i match con riga nel ledger: punteggi live usati
     come finali SOLO a >= SX_LIVE_MIN_AGE_MS (120') dal kickoff — sotto, il
     punteggio puo' ancora cambiare (fermate riprese il test che usava 1h).
  3) Le fonti esterne restano per i match ANCORA senza risultato sx-* (query
     diretta su match_results, tabella garantita da _create_results_table).
  Il percorso SX e' attivo SOLO se esiste almeno una fonte punteggi
  (ODDS_API_KEY o API_FOOTBALL_KEY) cosi' i test offline restano senza rete;
  disattivabile con `SX_NATIVE_SETTLEMENT=0`. Provider iniettabile
  (`settle_sx_bets(provider=...)`). Fail-safe: eccezioni catturate e
  loggate, mai chiuse righe senza punteggio reale.
  **Esito produzione 12/09 18:35 UTC**: bet #21 America MG–Nautico WON +2.25,
  #33 Jaguares–Fortaleza LOST -1.00, #34 Santa Fe–Tolima WON +1.46
  (netto +2.71 USDC, ZERO crediti the-odds-api). Restavano aperte solo le
  due del giorno: CSKA (si chiude coi punteggi live del percorso active)
  e Atalanta (kickoff 18:45, percorso normale). Test: 12 verdi in
  `test_sx_native_settlement.py` + autouse dummy provider in
  test_league_mapping (no rete) + regressioni settlement/bot/odds_api verdi.
  **Scoperta corollario**: the-odds-api NON copre Primera A/Nacional/K2
  (verificato su /v4/sports, 86 sport): NON aggiungerle a SPORTS_MAP — la
  copertura settlement di quelle leghe e' solo SX-native.

- **FIX regressione 71b2c4c (09/09, deployato c299abe)**: il cleanup OU2.5
  aveva rimosso il parametro posizionale `p_over` dalla chiamata a
  `tracker.save_analysis` in `fixture_engine._analyze_match` senza
  aggiornare la firma → ogni analisi partita crashava con TypeError
  ("missing 1 required positional argument: 'status'"), abortendo l'intera
  lega nel giro calendario (il prossimo giro 10/09 04:00 UTC sarebbe
  fallito). Ripristinato `p_over=None` (colonna legacy `prob_over` del
  ledger). Stessa regressione su bot.py: `format_segnale_pronto` non ha
  piu' `quota_over` ma `cmd_test_segnale`/`cmd_segnale` passavano ancora 7
  argomenti posizionali → `/segnale` e `/test_segnale` rispondevano
  "Errore nel calcolo. Riprova.". Ora i chiamanti usano keyword e
  `cmd_segnale` ha perso il lookup legacy delle quote Over (bookmaker
  sempre "Modello" con caveat). Ripristinata la costante `OU_ENABLED =
  False` in fixture_engine (documentata qui sotto, tripwire
  `test_ou_exclusion`); test aggiornati (test_bot: `test_escluso_over_under`
  asserisce l'ASSENZA di OU). Verificato: suite completa 831 test verdi,
  smoke test su produzione OK. NB: le analisi girano SOLO quando una lega
  e' "dovuta" (rotazione 3gg top leghe): dopo il 07/09 04:00 il prossimo
  giro con refresh e' il 10/09 04:00 — giorni senza analisi sono attesi
  per design.
- **Stagger rotazione quote (10/09)**: con le leghe core tutte a 3gg e
  cache sincronizzate, le analisi avvenivano solo 1 giorno su 3 (gli
  altri giorni `due` era vuoto → 0 match analizzati → auto-bet senza
  candidati freschi). `odds_api.is_sport_due` ora applica una FASE stabile
  per lega (hash → 0..intervallo-1): sul "giorno di fase" (cache con eta'
  >= 1gg) la lega core (intervallo <= 7gg) diventa dovuta anche prima
  della scadenza, spalmando le scadenze su giorni diversi → analisi
  GIORNALIERE a costo invariato (ogni lega resta sul suo intervallo; le
  leghe 30gg restano dormienti pure, zero costi extra). Test dedicati in
  test_odds_api.py (fasi distinte, scadenza su giorno di fase, 30gg non
  anticipate, intervallo mai allungato).
- **PRIMA SCOMMESSA LIVE REALE piazzata (09/09 18:36 UTC) + fix dei 3
  bug che bloccavano auto_bet (commit `ad50810`)** — il giro automatico
  (ogni 60s) ha piazzato la bet #18: **Charlton Athletic (1) @ 3.3333,
  1 USDC, FULLY_FILLED, bet_id reale SX** (prezzo MIGLIORE del segnale
  3.25: floor EV rispettato), wallet 47.16 → 46.16 USDC. Prima del fix
  il bot loggava "0 puntate" da giorni NONOSTANTE segnali value attivi
  nel ledger. Bug fixati:
  1) **`_today_value_picks` leggeva `match_analysis`** (solo best-per-EV,
     che può essere `rejected`) invece del ledger `predictions` (status
     per OGNI esito): Derby (best=Draw EV 0.151 rejected, ma Derby/West
     Brom value nel ledger) → 0 candidati → il bot non piazzava mai.
     Ora JOIN su `predictions` (mercato='1X2', status value/strong_value,
     esito_finale IS NULL), un pick per match = best EV tra i value.
  2) **`"over" in el` sul settlement** (`_prediction_outcome`/
     `_esito_won`/`_esito_possible`): 'Blackburn **Rovers**' contiene
     'over' come sottostringa → la pred 1X2 #98 veniva saldata come
     Over 2.5 (won coi gol 1-2) e bloccata dal sanity check per sempre.
     Ora il match OU scatta SOLO se l'esito inizia con 'over'/'under'
     come parola intera. Pred #98 risaldata: Blackburn `lost` -1.0.
  3) **TEAM_MAP senza alias** per Derby County/West Bromwich Albion/
     Cardiff City/FSV Mainz 05/Swansea City/Atalanta BC/AS Roma →
     `_canonical_esito` non risolveva quegli esiti. Alias aggiunti.
  Verificato live: `_prediction_outcome` Blackburn='lost'; `_today_value_
  picks` ora trova i candidati (Charlton, West Brom). Prossimi candidati
  in finestra: Union Berlin (11/09) e i 20+ segnali del 12/09 — il bot
  li piazzerà automaticamente appena entrano nella finestra mobile 24h.
- **AUTO-BET LIVE minuto-per-minuto + staking prudente (09/09)** —
  configurazione operativa richiesta dal proprietario: 1) **Frequenza**:
  `auto_bet_job` passa da ogni 3h a **ogni 60s** (`run_repeating`
  interval=60, first=60, `max_instances=1` anti-sovrapposizione) — il
  giro non brucia crediti the-odds-api (segnali dal DB + prezzi SX
  dall'API pubblica) e il floor EV cattura i miglioramenti di prezzo fino
  alla guardia 15 min pre-kickoff. 2) **Kelly FISSATO al 5%**: env
  `KELLY_MIN_FRACTION=KELLY_MAX_FRACTION=0.05` (la frazione dinamica
  0.05-0.40 resta nel codice, con MIN=MAX è sempre 0.05). 3) **Cap 2%**
  per singola operazione: `STAKE_CAP_PCT=0.02` e
  `STAKE_CAP_PCT_STRONG=0.02`. ⚠️ Il floor exchange 1 USDC prevale sul
  cap: con wallet ~47 USDC lo stake effettivo è 1 USDC (>2%); il cap 2%
  diventa vincolante da ~50 USDC in su (top-up consigliato). 4) Soglie
  EV invariate e già attive (+3% value, strong_value EV>8% + edge ≥+5pp)
  e drift watchdog ogni 6h invariato. Anti-spam: l'avviso kill-switch
  OFF (che col giro ogni minuto scattava 1440 volte/giorno) è limitato a
  **1 alert/giorno** (chiave `KS_OFF` su tracker.is_notified). Le env
  sopra sono DICHIARATE in `.railway/railway.ts` (preserve) insieme a
  AUTO_BET_MODE/EXECUTION_PROVIDER/SX_* per non farle distruggere da
  `railway config apply`; su Railway sono già impostate (l'esecuzione
  LIVE era già attiva). Startup invariato: `Dockerfile` → `run_all.py`
  (processo bloccante, i job vivono nella job_queue del bot).
  Test aggiornati: `test_football_scan_h24.py` ora impone interval=60.
- **ELO tennis superficie-specifico + time-decay nel sandbox (09/09)** —
  esteso il modello del `tennis_sandbox` prima di affidargli piu' superfici:
  1) **Surface-Specific ELO**: ogni giocatore ha un rating OVERALL + rating
  per superficie (hard=cemento/clay=terra/grass=erba). La superficie del
  torneo e' rilevata dai campi testo del mercato SX (`market_surface` →
  `detect_surface`, matching pesato su keyword di tornei; NIENTE `surface`
  in V3). Se il torneo non e' riconosciuto o il testo e' ambiguo → None
  (mai indovinare: una superficie sbagliata contaminerebbe il rating).
  Un match su superficie nota aggiorna overall + superficie; uno
  sconosciuto SOLO l'overall. Ledger migrato (ALTER idempotente): colonna
  `surface` su `signals` e `observations`. 2) **Time-decay 30/60 giorni**: i
  risultati invecchiano all'USO — la rating efficace regredisce verso il
  neutro 1500 in base all'eta' dell'ultimo match osservato: peso PIENO
  sotto `ELO_RECENT_DAYS` (30), ~50% a ~60gg (`ELO_HALF_LIFE_DAYS` 30),
  zero oltre `ELO_WINDOW_DAYS` (365). Il seeding dal mercato (n=0) non
  invecchia. 3) La probabilita' per i +EV usa la **blended rating**: con
  storico su quella superficie domina la superficie (peso
  `min(1, n_surf/SURFACE_MIN_MATCHES)`), senza storico l'overall. Il
  settlement passa la superficie dell'observation a `elo.update`, quindi le
  superfici imparano solo dai match su quella superficie. `ratings.json`
  RETROCOMPATIBILE (vecchio formato → overall, superfici vuote). Report
  con riepilogo per superficie (`🏟️ Per superficie`). Test dedicati
  (decay 30/60/365gg, isolamento superfici, blend, migrazione ledger,
  retrocompatibilita', rilevamento tornei).
- **Contenimento staking tennis sandbox (09/09)**: ridotti i default di
  staking per evitare puntate eccessive (osservata una stake paper 50 su
  Alcaraz) finche' l'EV non si stabilizza: `TENNIS_KELLY_FRACTION`
  0.25 -> **0.10**, `TENNIS_MAX_STAKE_PCT` 0.05 -> **0.02** (2% del
  bankroll virtuale) e NUOVO tetto assoluto `TENNIS_MAX_STAKE_ABS`
  (default **25**) in `kelly_stake` (min tra Kelly frazionato, cap %% e
  cap assoluto). Con bankroll 1000 lo stake paper massimo passa da 50 a
  20. Nessun override env su Railway: i nuovi default valgono dal deploy.
  Test aggiornati (TestKelly: 20 con nuovi default + tetto assoluto 25
  che prevale sul cap %% alto).
- **Calibrazione isotonica ATTIVA in produzione (09/09)**: soglia
  `MIN_CALIB_SAMPLES` abbassata da 60 a **50** in
  `probability_calibration.py` (il ledger era fermo a 57 chiusure: con
  30% OOF servivano comunque 17 punti di calibrazione). In piu' il retrain
  ora gira anche al BOOT (`run_once(retrain_ensemble_job, when=20)` in
  bot.py): al primo deploy che introduce la nuova soglia la calibrazione
  si attiva SUBITO, senza attendere le 05:45 UTC del giorno dopo (il job
  e' idempotente, zero API). Verificato in simulazione: 57 campioni ->
  calibrator fitted (n_cal=17, pre_brier 0.3163 -> post_brier 0.2412,
  ECE 0.3152 -> 0.0).
- **Drift watchdog in background (09/09, bot.py)**: nuovo job
  `drift_watchdog_job` ogni 6h (first=1800) che controlla la calibrazione
  rolling e allerta admin+iscritti SOLO su status="drift" (anti-spam:
  transizione a drift oppure 1 alert/24h se persiste; stato sempre
  loggato). Il retraining e' gia' coperto da 05:45 UTC + boot: l'alert e'
  il campanello, non l'azione. Nuovo endpoint **GET /api/drift** in
  web_api.py per la verifica REMOTA del drift (stesso check del job,
  comodo per cron/uptime esterni).
- **Dashboard calibrazione (09/09, webapp + API)**: nuovo endpoint
  **GET /api/calibration** in web_api.py che aggrega l'istantanea
  completa per la nuova pagina webapp `/calibrazione` (link nel menu):
  stato drift (stesso check di /api/drift), metriche training dell'
  ensemble (modello, acc, Brier, peso ML), stato calibrazione isotonica
  (pre/post Brier/ECE, n_cal, curva score->prob calibrata dal
  calibratore) e reliability diagram calcolato sulle previsioni chiuse
  (confidenza vs frequenza empirica, bin a larghezza uguale come l'ECE).
  La pagina disegna le curve in SVG puro (zero dipendenze chart: la
  webapp ha solo next/react) con fallback demo se il backend non e'
  raggiungibile. Test dedicati in test_web_api.py (TestCalibration +
  rotte registrate).
  **Grafico temporale del drift (09/09)**: nuova
  `drift_monitor.brier_history()` — serie walk-forward del Brier/LogLoss
  rolling calcolata a ogni chiusura (finestra 30, min 15), downsampling
  che mantiene SEMPRE l'ultimo punto (il valore corrente); esposta in
  /api/calibration come `drift_history` e disegnata nella pagina con
  baseline e soglia drift (1.30x baseline). Test: TestBrierHistory in
  test_drift_monitor.py. NB: `n` nei punti e' la dimensione della
  finestra rolling (cap 30), non il conteggio cumulato.
- **Retraining ensemble ML su produzione (09/09)**: eseguito a mano sul
  container Railway (`python3 ml_ensemble.py --retrain`) dopo drift
  rilevato dal monitor (Brier rolling 0.2432 vs baseline 0.2073, LogLoss
  0.6789 vs 0.6049, 49 previsioni chiuse): Brier di training 0.2403 ->
  **0.0354**, acc 0.982, n=57 righe, XGBoost, `ensemble_weight` 0.51,
  salvato su /app/data/ensemble_model.json. Calibrazione isotonica
  ancora `skipped` (57 < 60 MIN_CALIB_SAMPLES: si attiva da sola col
  ledger che cresce). NB: il Brier di training NON e' il Brier rolling
  del drift monitor (quello misura le ultime 30 chiusure fatte dal
  modello VECCHIO e resta valido finche' non si chiudono nuove
  previsioni col modello nuovo).
- **Collaudo LIVE calcio verificato (09/09)** — su Railway il sistema era
  GIÀ in esecuzione live effettiva: env `AUTO_BET_MODE=live` +
  `EXECUTION_PROVIDER=sxbet` (credenziali SX presenti), file kill-switch
  `data/execution/auto_bet_mode.json` ASSENTE → a runtime
  `kill_switch_status()` = {override: null, env_mode: live, effective:
  live, provider_ready: true}. Il giro `auto_bet_job` delle 16:42 UTC ha
  loggato "bankroll LIVE = saldo wallet 12.28 USDC" e 0 ordini: NON c'e'
  alcun gate a ~100 campioni che blocchi il live (nel percorso auto_bet
  non esistono soglie di campioni: ensemble 30 / calibrazione 60 / drift 15
  sono solo telemetria) — i 0 ordini dipendono dal filtro value: nelle 24h
  c'erano solo 7 match analizzati, 6 `rejected` e 1 `value` debole
  (Cardiff–Stoke @3.25, kickoff 18:45 UTC, perso dalla guardia dei 15').
  Le puntate sim piu' recenti nel DB restano quelle del 05/09 (Over 2.5,
  pre-esclusione OU). Prossimo giro ogni 3h (es. 19:42 UTC) dopo le
  analisi 18:00 UTC.
- **TRANSIZIONE LIVE UFFICIALE micro-staking calcio 1X2 (09/09)** —
  direttive operative confermate con verifica sul container:
  1) LIVE: env `AUTO_BET_MODE=live` + `EXECUTION_PROVIDER=sxbet` +
     credenziali SX, kill-switch ASSENTE, `kill_switch_status()` =
     {effective: live, provider_ready: true}, `ExecutionEngine()` =
     `SxBetProvider` REALE (mai DryRun), **saldo 47.16 USDC** disponibili
     (proxy 0x97aE…44002, exposure 0), staking `AUTO_BET_STAKE_MODE=
     adaptive` (env flat RIMOSSA, `EXECUTION_DRY_RUN` assente). Zero
     ordini reali finora: NON per configurazione (tutto live) ma perche'
     nessun segnale +EV ha superato i gate dal passaggio a live — il
     primo ordine arrivera' al primo segnale value che passa il filtro
     (es. candidato odierno Watford–Stoke 1 @2.32 EV +15%). Le perdite
     reali del ledger (mode='live') alimenteranno da subito retraining
     (05:45 UTC + boot) e ML: feedback strutturale, niente conteggi fissi
     di giocate/giorno (solo segnali +EV + risk cap 40%/30%).
  2) TENNIS sandbox: limiti ridotti CONFERMATI nel container — env
     `TENNIS_KELLY_FRACTION`/`TENNIS_MAX_STAKE_PCT`/`TENNIS_MAX_STAKE_ABS`
     ASSENTI → valgono i default 0.10 / 0.02 / 25 (stake paper max 20 su
     bankroll 1000); `TENNIS_SANDBOX_ENABLED=1`.
  3) MONITORAGGIO invariato: calibrazione isotonica fitted (Brier OOF
     0.2322→0.1479, ECE→0), drift watchdog ogni 6h, retrain 05:45 UTC +
     boot, `GET /api/drift` + `/api/calibration` attivi.

- **Sandbox tennis + scansione calcio H24/7 (08/09)** — nuovo modulo
  `tennis_sandbox.py` (pattern surebet_engine: indipendente da tracker/bot,
  ledger SQLite dedicato in `data/tennis_sandbox/`, mai import da tracker/bot
  e nessuna credenziale — test dedicati lo verificano). Legge i mercati
  tennis Moneyline (type 52 = "12", sportId 6) dall'API PUBBLICA di SX Bet
  (zero costi, zero ordini: fail-closed totale). Baseline **Weighted ELO**
  (K pesato per recency, seeding a COPPIA coerente — il seeding singolo
  distorceva prob: 0.773→0.920) seminata dalle probabilita' implicite del
  mercato e aggiornata SOLO dai risultati reali delle osservazioni saldate
  (`/markets/find` → `outcome` 1|2|0). Anti-EV-spurio: filtro coerenza
  mercato `MIN_INV_SUM=0.98/MAX_INV_SUM=1.08` (somma inversi dei due best
  back ~1 su un exchange; sotto soglia = book sporco, l'EV finto supera
  l'EV_MIN 3% solo con edge reale del modello). Ledger: ogni +EV registrato
  con selezione univoca (UNIQUE market_hash+selection), stake Kelly
  frazionario su bankroll VIRTUALE (paper, default 1000) e OGNI match
  scansionato salvato come observation per l'apprendimento ELO (senza
  settlement l'ELO resterebbe identico al mercato: deadlock). Report
  `--report [--json]`: opportunita'/giorno, ROI teorico, win rate. CLI:
  `--scan`, `--settle`, `--loop N`. Attivo su Railway con
  `TENNIS_SANDBOX_ENABLED=1` (scan+settle ogni 6h + report 05:55 UTC via
  Telegram); collaudo reale 08/09: 8 mercati US Open, 8 book, 1 segnale +EV
  (Linda Noskova @4.21 vs rating seminato — mercato in movimento).
- **Scansione calcio H24/7 garantita da test** (08/09): il percorso 1X2
  (bot.py morning/afternoon/evening + fixture_engine + odds_api) non ha
  alcun gating per giorno della settimana — tripwire
  `test_football_scan_h24.py` blocca chiunque introduca limitazioni "solo
  weekend" (job run_daily senza `days=`, assenza di isoweekday/weekday/%7
  nella pipeline, auto_bet run_repeating 3h).

- **auto_bet → execution_engine LIVE (08/09)**: i segnali value/strong_value
  del job 08:50 possono ora piazzare ORDINI REALI su SX Bet (uscita dal
  SIM-only). Si attiva solo con `AUTO_BET_MODE=live|real` E provider reale
  configurato (`EXECUTION_PROVIDER=sxbet` + `SX_API_KEY`/`SX_PRIVATE_KEY`);
  senza, resta SIM (default) o fail-closed con allow_sim=False. Flusso:
  risoluzione del mercato exchange per la STESSA partita (nomi squadre +
  kickoff, univocità — mai ordini su eventi ambigui,
  `execution_engine.resolve_match_market`, provider sxbet), floor EV (si
  riempie SOLO alla quota-segnale o meglio: best < quota → salto), ordine
  IOC con bound = quota del segnale, ledger `bets` con mode='live' +
  market_id/selection_id/bet_id reali e stake/prezzo MATCHED. Gli ordini
  rifiutati o i salti non lasciano righe (un FAILED verrebbe saldato come
  perdita). Risk caps invariati (correlation 30% + esposizione 40%).
- **Staking 100% dinamico (08/09)**: rimosse le regole fisse (era minimo
  2.00 EUR / step 0.50). Il Kelly frazionato (0.05-0.40, env
  `KELLY_MIN_FRACTION`/`KELLY_MAX_FRACTION`) calcola lo stake per OGNI
  scommessa sul bankroll corrente: in LIVE il bankroll è il SALDO REALE
  del proxy wallet SX (`get_balance` → `availableBalance`), non la cassa
  simulata (se il saldo è < minimo ordine → nessuna puntata, fail-closed).
  Floor = minimo ordine exchange (`STAKE_MIN_EUR`, default 1.0 = 1 USDC),
  step 0.01 (`STAKE_STEP_EUR`), cap per singola bet `STAKE_CAP_PCT` 10%
  (value) / `STAKE_CAP_PCT_STRONG` 25% (strong_value): esposizione solo
  sui segnali a forte margine. Kelly riduce naturalmente lo stake sulle
  quote ad alta varianza (formula piena). `/autobet now` esegue il giro
  SUBITO da Telegram (senza attendere il job 08:50).
- **Auto-bet 24/7 (08/09)**: il giro puntate gira OGNI 3h (da 08:50 ITA,
  `run_repeating` in bot.py) invece di una sola volta al giorno: i nuovi
  segnali value delle analisi (04:00/12:00/18:00 UTC, finestra candidati
  mobile 24h) vengono scommessi entro 3h, giorno e notte. Sicurezza:
  UNIQUE(match_id, esito) evita doppioni, guardia 15 min evita ordini a
  partita iniziata, e il cap esposizione TOTALE è GIORNALIERO e
  multi-giro (`auto_bet._today_placed_stake` sottrae l'esposizione già
  piazzata nei giri precedenti dentro `apply_total_exposure_cap`): con
  più giri al giorno il 40% di bankroll vale sul giorno intero, non per
  singolo giro.
- **Fix loop infinito football_hist (08/09)**: il ramo "retry" di
  `sync_history` faceva `continue` senza limite sulla stessa stagione
  (osservato in produzione: "Retry stesso anno per Serie A 2024" a ~50
  righe/sec per oltre 25 min, bloccando il job e bruciando la quota
  API-Football). Aggiunto `MAX_YEAR_RETRIES` (3) con sleep tra i tentativi:
  dopo il limite la stagione è trattata come non accessibile e si passa
  all'anno precedente (mai più blocchi). Test dedicati.
- **Kill-switch Telegram `/autobet` (08/09)**: comando admin che blocca
  le puntate automatiche da remoto in emergenza. Override PERSISTENTE in
  `data/execution/auto_bet_mode.json` (volume condiviso, sopravvive ai
  redeploy) con precedenza su `AUTO_BET_MODE` env: `/autobet off|stop` =
  STOP TOTALE (nessuna puntata, né reale né simulata), `/autobet sim|pause`
  = pausa ordini reali (resta paper trading), `/autobet live|resume` =
  ripristina l'env, `/autobet` = stato (effective/override/env/provider).
  Se il giro 08:50 viene saltato per kill-switch OFF, il job notifica
  l'admin. Test dedicati in test_auto_bet_killswitch.py.
- **Check proxy wallet SX (08/09, live su Railway)**: `execution_engine.py
  --balance` (nuova flag CLI) legge `user/balance-v3`: proxy wallet
  `0x97aE...44002` deployato e finanziato, **12.28 USDC disponibili**,
  exposure 0, nessun ordine aperto (la prova Nueva Chicago saldata).
  ⚠️ Saldo basso: con 2-3 segnali value da €2-5 il wallet potrebbe non
  coprire tutti gli stake (ordini in eccesso falliscono in modo
  fail-closed, nessuna riga sul ledger) — valutare un top-up prima di
  affidarsi all'automazione 24/7.
- **Correzioni ML post-diagnostica (06/09)** — il report sul backtest
  storico (12.909 partite) mostrava: ROI -2,52% flat, controllo a quota
  CLOSING -3,03% (la selezione NON batte il mercato devigato),
  overconfidence crescente sui bucket alti (0.5-0.6: hit 49% vs 55 atteso;
  0.6-0.7: 56% vs 65; 0.7-1.0: 67% vs 85), CLV vig-free negativo ovunque
  e leak sistematico sull'OU2.5 (-6,8% su 924 bet, under -7,3%). Tre
  azioni eseguite (commit in corso):
  1) **OU2.5 ESCLUSO DEFINITIVAMENTE dalle selezioni (06/09)** — `fixture_engine`
     non genera piu' candidati Over/Under, SENZA escape hatch (rimosso
     `ENABLE_OU_MARKET`; `OU_ENABLED` è costante False). Il sistema elabora
     SOLO segnali 1X2. Il ledger previsioni smette di imparare dal mercato
     perdente.
  2) **Ensemble ML ATTIVATO in produzione** — prima non esisteva
     `data/ensemble_model.json` sul volume Railway: `get_ensemble()`
     tornava non addestrato e le analisi usavano solo Poisson+blend.
     Aggiunto `ml_ensemble.reset_ensemble_cache()` + job schedulato
     `retrain_ensemble_job` (bot.py, 05:45 UTC): addestra dal ledger
     live (`build_training_rows`, zero API) e salva il modello sul
     volume se il dataset supera MIN_SAMPLES=30 (oggi ~40 righe chiuse:
     il file viene creato al primo giro / primo retrain manuale).
  3) **Shrink sui bucket alti (esperimento, NON in produzione)** — in
     `historical_backtest` (harness di ricerca, flag
     `--high-prob-threshold/--high-prob-shrink`): sopra soglia 0.55 la
     deviazione dal mercato viene compressa del fattore 0.85.
     **Esito misurato (catena di 4 run flat €20 comparabili, 06/09):**
     - baseline OU ON: n=6274, flat -7,99%, closing **-8,32%**;
     - shrink .85 OU ON: n=5297, flat -6,17%, closing -6,62% (aiuta
       col mercato OU attivo: taglia le pick OU marginali, il leak);
     - baseline NO-OU (**config produzione dopo Punto 3**): n=1262,
       flat -6,20%, closing **-6,11%**;
     - shrink .85 NO-OU: n=1305, flat -9,28%, closing **-9,31%**
       (**PEGGIORA**).
     Perche': nella config 1X2-only i bucket alti (0.6+) contengono
     appena 7-9 bet — lo shrink non ha dove agire e comprimendo i
     favoriti ("1") sposta la selezione verso i pareggi/trasferte
     sovraconfidenti (bucket 0.3-0.4: 680 bet, hit 29.4% vs 35 atteso).
     → `value_filter` NON ha ricevuto lo shrink alto. Il gap residuo
     della config 1X2-only e' il bucket BASSO (X/2), non l'alto.
  4) **PATCH CALIBRAZIONE bucket bassi (06/09, IN PRODUZIONE)** — in
     `value_filter.adjusted_probability`: sotto LOW_PROB_THRESHOLD (0.40)
     la deviazione dal mercato viene compressa del fattore LOW_PROB_SHRINK
     (0.85). Misurata sul backtest (run flat €20 NO-OU): closing
     **-6.11% -> -3.08%**, flat -6.20 -> -3.28, n_bets 1262 -> 694
     (le pick X/2 marginali escono dal filtro EV), strong_value
     -0.3% -> **+6.0%**, pocket X +2.6% -> +14.4%, "2" trasferta
     -21.9% -> +22.1% (n=54, campione piccolo ma direzione coerente).
     Nota: il vecchio report "closing -3,03%" del 05/09 era su codice
     precedente (pre-0c9c4bb) e NON e' confrontabile; il baseline
     confrontabile della config produzione e' il run flat NO-OU.
- **Audit crediti the-odds-api + PROFILO SETTEMBRE SOTTO-BUDGET (05/09)**:
  misura live del 05/09 ~16:10 UTC → **239/500 usati, ~260 residui** per
  ~25 giorni (~10,3/giorno sostenibili). Il vincolo originale (~460/mese)
  conteggiava SOLO la rotazione quote: i costi `fetch_scores` del
  settlement (~12-16/giorno) e del surebet NON erano mai stati conteggiati.
  Tagli applicati per arrivare a fine mese SENZA interrompere il bot:
  1) settlement mirato (`get_leagues_with_open_rows`, solo leghe con
     scommesse attive/chiuse da <48h su partite iniziate) + watchdog da 2h
     a 4h (commit 6603920);
  2) rotazione value ridotta a ~3,5/giorno: SOLO i top campionati a 3gg
     (Serie A, PL, La Liga, Bundesliga, Ligue 1, Eredivisie, Serie B, EFL
     Champ) + coppe europee/mercati maggiori a 7gg (CL, EL, MLS,
     Brasileirao, Liga MX, Saudi); TUTTO il resto a 30gg (dormiente fino a
     ottobre, cache del 1° settembre ancora fresca);
  3) surebet: solo MLB a settembre (in stagione, partite ogni giorno =
     campo-test continuo) con TTL 6h invariato; NBA off-season riattivata
     il 1° ottobre.
  Budget atteso: settlement ~2-3 + surebet ~4 + value ~3,5 ≈ **10-10,5
  crediti/giorno** → copre settembre. ⚠️ **RIPRISTINARE il 1° ottobre**:
  tabella SPORTS_INTERVAL_DAYS completa (git log) + `SUREBET_SPORTS`
  `"baseball_mlb"` → `"basketball_nba,baseball_mlb"` (reset crediti + stagione NBA).
- **Audit crediti 09/09 + fix H2H-only (dimezza il costo quote)**: timeline
  remaining reale: 09/01 13:05 → 488 | 09/03 → 372 | 09/04 → 310 |
  09/07 04:00 → 191 | 09/09 16:47 → **127** (~25/giorno tra 07/09 e 09/09,
  sopraelevato dal doppio addebito `markets=h2h,totals`). **Causa radice
  trovata**: the-odds-api addebita `markets × regions` per chiamata, quindi
  `h2h,totals` su 1 regione = **2 crediti** a fetch. Dal 06/09 il mercato
  totals (OU) è ESCLUSO dalle selezioni → richiederlo era puro spreco.
  Fix (commit `d1f1c18`): `markets="h2h"` only (tripwire
  `test_ou_exclusion.test_odds_request_solo_h2h`), deploy verificato sul
  container (`8887b972`). Giro serale 09/09 18:00 UTC verificato: 5 partite
  analizzate (La Liga + Serie B) con cache esistenti (stagger fase + TTL
  non scaduto → **0 crediti** consumati dal giro) e settlement da cache
  scores fresche; remaining invariato a 127. Prossimo fetch reale con
  h2h-only alla prima scadenza TTL (10/09 04:00 UTC → 1 credito/lega
  invece di 2). Budget residuo ~127 per ~21 giorni ≈ **6/giorno sostenibili**:
  con settlement 2-3 + value 3,5 (h2h-only) il consumo rientra, ma la
  rotazione va tenuta d'occhio (niente aggiunte fino al reset del 1° ottobre).
- **Calibrazione isotonica dell'ensemble** (04/09, `probability_calibration.py`):
  PAVA numpy-only (zero deps, coerente col progetto) che mappa gli score
  grezzi di XGBoost/LR sulle frequenze EMPIRICHE del dataset storico.
  Fit su split OUT-OF-SAMPLE (30% riservato, seed fisso — mai sui dati di
  training, altrimenti impara l'overfit e non corregge nulla); attiva con
  >=60 campioni chiusi (MIN_CALIB_SAMPLES). Integrata in `ml_ensemble.py`
  (train/predict/save/load + flag `calibrated` nel predict) e metriche
  Brier/ECE pre/post nel report di training (`metrics["calibration"]`).
  Oggi 9 previsioni chiuse → si attiva da sola col ledger che cresce.
- **CLV → staking WIRED** (04/09, auto_bet.py): `has_clv_positive` veniva
  letto ma MAI passato ad `adaptive_stake` (il parametro esisteva da
  sempre) — ora arriva dal CLV rolling di `clv_history`: CLV positivo =
  conferma dell'edge → frazione Kelly piu' alta. Test di wiring dedicati.
- **Cap esposizione totale** (04/09, auto_bet.py): `TOTAL_EXPOSURE_CAP_PCT`
  = 0.40, applicato in FASE 2 DOPO il correlation cap — il portafoglio del
  giorno non supera il 40% del bankroll (varianza additiva tra pick
  indipendenti), scaling proporzionale che preserva il ranking EV.
- **Test motore Poisson/Dixon-Coles** (test_poisson_engine.py): verifica
  della correzione rho (rho<0 → draw piu' alto del Poisson puro),
  coerenza 1X2/OU/BTTS/AH (somma 1), integrazione `expected_goals`.
- **Correlation risk cap** (03/09, auto_bet.py): Kelly assume indipendenza
  tra le puntate ma esiti correlati nello stesso blocco temporale (stessa
  partita 1X2+OU, oppure stessa lega con kickoff entro 90') condividono la
  varianza → `apply_correlation_cap` riduce PROPORZIONALMENTE gli stake
  quando l'esposizione del blocco supera il 30% del bankroll (mantiene il
  ranking EV, non taglia esiti). Gruppamento per LEGA + finestra temporale
  greedy (match_id uguale = sempre stesso blocco). Test dedicati in
  test_auto_bet.py (blocchi disgiunti senza cap, ranking preservato, flusso
  SIM completo).
- **Drift monitor del modello** (03/09, drift_monitor.py): Brier/LogLoss
  ROLLING sulle ultime 30 previsioni chiuse vs baseline storica; alert
  `drift` se il Brier rolling supera 1.30x la baseline o +0.03 in valore
  assoluto → raccomanda il retraining dell'ensemble ML. Sezione 🧠 nel
  report giornaliero (bot.py, fail-safe try/except) + CLI
  `venv/bin/python drift_monitor.py [--json]`. Stato "insufficient" sotto
  le 15 previsioni chiuse (oggi ~8: si attiva da solo col ledger che cresce).
  Migrazione idempotente `settled_at` su predictions/bets in tracker.py
  (stessa convenzione della cassa).
- **Sanity check settlement** (03/09): tripwire anti-contraddizione nei tre
  settle (bets/predictions/cassa): gol negativi/non numerici/`result`
  incoerente → la riga NON si chiude (resta aperta + log); verdetto 'won'
  impossibile coi gol (es. esito 2 con vittoria casa) → bloccato. Inoltre
  `settlement_sanity_check()` + `heal_settled_contradictions()` nel job di
  settlement (`_update_results`, watchdog 4h + job serali): se una riga già
  chiusa ha un verdetto che contraddice i gol CORRENTI (caso Machida: bet
  marcata won dopo correzione punteggio), viene riaperta e ri-saldata
  automaticamente, con alert Telegram `🔔 SANITY CHECK SETTLEMENT`.
  `settle_cassa` ora aggancia le coppie squadre con prefissi club loose
  (CA Osasuna=Osasuna) e preferisce il match PIÙ RECENTE della coppia.
- **Fix bankroll reale** (03/09): `bankroll_stats`/`get_peak_bankroll`/
  `_bankroll_stats` usavano `SUM(cassa.amount)` ma la colonna è `importo`
  → bankroll sempre 0/fallback (auto_bet usava €100 fisso). Corretto;
  dashboard e schedina ora mostrano il bankroll reale (default solo se
  cassa vuota).
- **Dashboard webapp estesa** (03/09): nuove sezioni Calibrazione per
  mercato (ROI vs EV atteso), Puntate automatiche, CLV (raw/vig-free/
  vs Pinnacle) e streak — dati reali da `/api/dashboard` (performance_report).
  Layout con evidenziazione della pagina attiva.
- **Report giornaliero**: nuova sezione `📊 Stato` con streak attuale
  (vittorie/perse di fila + max) e bankroll reale con peak/drawdown.
- **Deploy Railway**: Online, volume montato, bot in polling, health 200.
  **Consegna notifiche Telegram VERIFICATA** (test end-to-end 01/09):
  `POST /api/test_notify` con la chiave giusta ha consegnato il messaggio al
  Chat ID proprietario `7718157436` (ADMIN_CHAT_ID corretto su Railway).
  Endpoint protetto da `TEST_NOTIFY_KEY` (variabile Railway, mai nel repo).
  Bug storico fixato nello stesso giro: `format_schedina` mancava
  di `get_pro_stake` import → la schedina delle 08:00 non partiva con picks.
  **Verdetti puntate a fine partita**: `settle_bets(return_details=True)`
  restituisce i verdetti appena emessi e i job (pomeriggio/sera/23:30 e
  `/risultati`) inviano la notifica `🔔 ESITO PUNTATE AUTOMATICHE`
  (✅ VINTA/❌ PERSA/⚪ PUSH con P/L) a iscritti+admin.
- **Settlement watchdog / self-healing** (`bot.py`, ogni 4h dal 05/09, era
  2h): scarica i risultati e salda bet/previsioni/cassa aperte anche fuori
  dai job serali, con notifica verdetti. Copre redeploy che saltano i job,
  cache stantie, API lente. VERIFICATO IN PRODUZIONE (01/09): 3 bet
  pendenti (Birmingham, Wycombe, Tranmere) saldate al primo giro.
- **Refertazione credit-saving (05/09, `_update_results`)**: il settlement
  interroga `fetch_scores` SOLO per le leghe con scommesse ATTIVE
  (predictions/bets con `esito_finale IS NULL`, o chiuse da <48h per la
  finestra di verifica/heal) su partite GIÀ INIZIATE (5 giorni). Prima si
  interrogavano tutte le leghe con un segnale value negli ultimi 3 giorni:
  leghe con sole partite future o righe chiuse da giorni bruciavano crediti
  senza saldare nulla. Zero scommesse attive = zero chiamate per quella
  lega. Helper `tracker.get_leagues_with_open_rows` + test dedicati in
  test_settlement_watchdog.py. Watchdog da 2h a 4h (il referto non serve
  istantaneo).
- **Fix cache punteggi** (`odds_api.fetch_scores`): la cache non serve più
  partite iniziate da >3h con `completed=False` (cache scritta a partita in
  corso = stantia): refresh forzato, fallback cache solo se l'API fallisce
  (mai meno dati di prima). Causa radice delle bet rimaste aperte 24h.
- **Logging sicuro** (`secure_logging.py`, integrato in run_all/web_api/bot):
  filtro su tutti gli handler che maschera token/chiavi/segreti nei messaggi
  di log (raccoglie i valori da `os.environ` al bootstrap) + `httpx` a
  WARNING (prima loggava gli URL Telegram col token in chiaro). Verificato:
  0 occorrenze del token nei log del nuovo deployment.
- **.dockerignore rinforzato**: `secrets/`, `.env*`, `data/`, `*.key`,
  `*.pem`, `.git/` esclusi dall'immagine (il Dockerfile fa `COPY . .` — prima
  i segreti locali sarebbero finiti nell'immagine Docker).
- **Esecuzione via provider (06/09) + Smarkets e SX Bet (07/09)**: il
  tripwire `test_betfair_removed.py` è stato RIMOSSO (deciso dal proprietario)
  e nasce `execution_engine.py`: interfaccia Python unica
  (`ExecutionProvider`) verso BetInAsia BLACK / MollyBet (protocollo
  Betfair-compatible JSON-RPC, SportsAPING/v1.0, credenziali SOLO da env
  `EXECUTION_APP_KEY/USERNAME/PASSWORD`), **Smarkets** (API REST v3
  `https://api.smarkets.com/v3/`, credenziali `SMARKETS_USERNAME/PASSWORD`,
  base `SMARKETS_API_BASE`; login `POST sessions/` → header
  `Authorization: Session-Token`; prezzi ×1e4, quantità in stake ×1e4,
  buy=BACK/sell=LAY, 1X2 = event `football_match` / market `match_odds` /
  contratti Home/Draw/Away) e **SX Bet** (V3 dal 26/08/2026: REST
  `https://api.sx.bet` su SX Rollup/Arbitrum Orbit chainId 4162, testnet
  `SX_API_BASE=https://api.toronto.sx.bet`; letture pubbliche, scritture con
  header `x-sx-api-key` (`SX_API_KEY`), ordini firmati EIP-712 con la chiave
  privata dell'EOA `SX_PRIVATE_KEY` — richiede `eth-account` in
  requirements.txt, import lazy; 1X2 calcio = market type 1 binario "X vs
  Not X" Home/Tie/Away, `percentageOdds` prob ×1e20 ladder 0.125%,
  `totalBetSize` in unità USDC 6 decimali, IOC/FOK = take, GTC = rest;
  ⚠️ i fondi stanno nel proxy wallet dell'account, NON nell'EOA).
  `DryRunProvider` senza
  credenziali e probe a stake minimo (`EXECUTION_MIN_STAKE_EUR`=1€) che
  misura latenza e slippage reali loggandoli in
  `data/execution/measurements.jsonl`; discovery `--markets` per elencare i
  match odds calcio con i selection/contract id. ⚠️ `api.smarkets.com` è
  inibito dall'ADM in Italia (DNS → sito-inibito-giochi.adm.gov.it): il
  probe reale va eseguito da Railway/rete non italiana; `api.sx.bet` invece
  è raggiungibile dall'Italia (verificato 07/09). Health mostra
  `betfair_enabled: false` fisso (compatibilità
  frontend); `/api/scan` resta 503 "betfair_removed" (endpoint rimosso).
- **Refertazione SOLO the-odds-api**: risultati e saldaggio bet/previsioni/
  cassa passano ESCLUSIVAMENTE da `odds_api.fetch_scores` +
  `match_scores_by_name` (la stessa chiave delle quote restituisce i
  risultati FINITI della stagione corrente, aggancio diretto ai match_id
  the-odds-api già in `matches`). TENTATIVO API-Football il 04/09 e
  REVERSO nello stesso giorno: il piano free copre solo le stagioni
  2022-2024 (errore API "Free plans do not have access to this season"),
  quindi NON può saldare le partite correnti del 2026 — verificato con
  chiamata reale su copia del DB di produzione (0 match aggiornati).
  API-Football resta solo per lo storico ratings in `football_hist.py`.
  Vincolo garantito da `test_settlement_source.py` e dai test di
  settlement (patch del confine `odds_api.fetch_scores`).
- **auto_bet SIM-only permanente (04/09)**: il job 08:50 piazza puntate
  SIMULATE con la quota del segnale (mode='sim', niente conto Exchange);
  alimenta ledger/ML/CLV come prima, saldate a fine partita. I candidati
  del giorno usano una FINESTRA MOBILE di 24h (non il giorno calendario
  UTC): a fine giornata un match con kickoff poco dopo la mezzanotte
  cadrebbe nel giorno dopo e verrebbe perso dal filtro per data.
- **Bugfix 02/09**: `auto_bet.run_today_bets` senza guardia `stake <= 0` sul
  percorso a stake fisso (possibile bet da €0.00); `web_api._schedina_json`
  con UnboundLocalError (`_bankroll` locale oscurava la funzione modulo →
  `/api/schedina` rotto con adaptive attivo). Test aggiornati all'era
  adaptive: percorso fallback testato con stub `sys.modules["adaptive_staking"]
  = None` + nuovo test del wiring adaptive (stake Kelly usato davvero).
- **Dataset ML** (`ml_dataset.py`): export CSV di addestramento da
  predictions+bets JOIN match_analysis (lam_h/lam_a, prob 1/X/2/O) e
  match_results → righe con label_ml (1=vinta). CLI
  `venv/bin/python ml_dataset.py` (→ data/training_dataset.csv) e
  `GET /api/training` (JSON, limit; es. ?limit=500).
- **Audit qualita' dataset ML** (`ml_audit.py`): un dataset sporco viene
  IMPARATO dal modello come verita' — controllo automatico di ogni riga
  (esito_finale valido, label_ml coerente, quota>1, prob in [0,1], profit
  col segno giusto, esiti strutturati per OU/AH/BTTS, duplicati). CLI
  `venv/bin/python ml_audit.py [--source predictions|bets]` (exit 0=ok,
  1=problemi) e **integrato nel report giornaliero**: `format_daily_report`
  audita le previsioni/puntate chiuse nel periodo e segnala i problemi
  (per tipo + primi esempi) in `/riepilogo` e nei report automatici.
- **Vault segreti**: attivo da locale (vault.bin Fernet/PBKDF2, 6 segreti
  cifrati, plaintext cancellati) — vedi sezione "Segreti".
- **Cassa**: funziona con doppia persistenza (localStorage + backup server sul
  volume). Endpoint: `GET/POST/DELETE /api/cassa`. **Ora si SALDA da sola**
  (`settle_cassa` in tracker.py): esito_finale/profit/settled_at, P/L reale e
  ROI in `/risultati` e nella pagina Cassa del sito.
- **Ledger previsioni** (tabella `predictions`): TUTTI i segnali proposti dal
  motore (1X2, Over/Under, Asian Handicap) vengono registrati con `mercato`,
  saldati a fine partita (`settle_predictions`, split-bet AH quarter incluso)
  e aggregati per mercato (`predictions_summary`) → telemetria di calibrazione
  in `/risultati`, `/backtest` e `/api/dashboard` (`per_mercato`).
- **Asian Handicap**: motore in `poisson_engine.ah_outcome_probs` (linee
  ±0.25…±3, push/split), parsing mercato `spreads` in fixture_engine
  (line shopping + devig power + blend + filtro EV). Solo telemetria per ora:
  il segnale della schedina resta 1X2/OU.
- **Quote**: fix fallback `load_odds(path)` (prima non funzionava mai) e nota
  di freschezza in `/segnale` quando le quote sono da cache vecchia.
- **Puntate automatiche** (`auto_bet.py`, job 08:50 ITA): SIM di DEFAULT
  (quota del segnale, mode='sim') oppure **LIVE via execution_engine dal
  08/09** con `AUTO_BET_MODE=live` + provider reale (SX Bet; ordini reali
  con floor EV e risoluzione evento univoca, mode='live' nel ledger).
  Stake **ADATTIVO** di default (`adaptive_staking.py`):
  Kelly frazionato dinamico (0.10-0.35 vs 0.25 fisso prima) con drawdown
  protection (>10% drawdown → riduzione stakes) e confidence weighting
  (market_edge alto + strong_value → stake più alto). Cap: 3% value, 5%
  strong_value. Fallback: stake fisso `BET_STAKE_EUR` se modulo assente.
  **Flat-stake opzionale (09/09)**: con `AUTO_BET_STAKE_MODE=flat` ogni
  segnale +EV del Calcio 1X2 viene piazzato a `AUTO_BET_FLAT_STAKE_EUR`
  (default 1 USDC = minimo ordine SX Bet); i risk cap restano attivi ma
  a UNITA' INTERE (`apply_flat_budget`: max floor(30% bankroll) segni per
  blocco correlato e floor(40% - già piazzato) segni/giorno, EV-decrescenti
  — niente frazioni non piazzabili, il minimo SX è 1 USDC).
  ⚠️ **RIPRISTINATO IL KELLY DINAMICO il 09/09 sera** dopo il deposito
  Bybit→SX a **~47,16 USDC** (verificato live su Railway:
  `availableBalance 47163372`; env `AUTO_BET_STAKE_MODE=adaptive`, env
  flat rimossa). Esempi su 47,16 USDC: value €2,99 / strong_value €5,68
  con i default prudenti (Kelly 0,05-0,40, cap 10%/25%, floor 1 USDC,
  esposizione 40% ≈ €18,9/giorno, correlazione 30% ≈ €14,2/blocco,
  drawdown −10% → stake −50%).
  Guardie: salta partite a <15 min dall'inizio, doppie puntate (UNIQUE
  match_id+esito). Risk caps prima del salvataggio: correlation cap (30%
  bankroll per blocco correlato) + cap esposizione totale (40%). Registro
  in tabella `bets`, saldato a fine partita (`settle_bets`) e incluso nel
  riepilogo.
- **Report giornaliero**: `/riepilogo [oggi|ieri|YYYY-MM-DD]` + invio
  automatico all'alba (06:05 ITA, riepilogo di ieri) e **a fine ultima
  partita** (check ogni 15' dalle 21:00 ITA, fallback notturno 23:50 ITA):
  previsioni chiuse per mercato (ROI vs EV), cassa saldata, puntate auto
  (P/L), CLV raw + **CLV vig-free** (devigato, piu' accurato) + CLV vs
  Pinnacle (closing line sharp), e alert chiavi mancanti.
  **Timezone**: i job usano UTC; `IT_OFFSET=2` converte gli orari in italiani
  (cambiare a 1 a fine ottobre per ora legale invernale).
  Destinatari: iscritti (`/subscribe`) **+ sempre** i chat in `ADMIN_CHAT_ID`
  (proprietario, virgola-separati). `/myid` mostra il proprio Chat ID.
- **Sticker premium**: inviato prima dei messaggi premium (set pubblico
  `PREMIUM_STICKER_SET`, default "Diamond") — workaround gratis alle custom
  emoji (che richiederebbero Fragment o Premium sull'account proprietario).
- **Copertura MONDIALE (66 competizioni)**: SPORTS_MAP (odds_api.py)
  interroga TUTTE le competizioni di calcio the-odds-api (chiavi ufficiali
  verificate sul sito): top campionati + serie B + coppe europee/internaz.
  + nazionali.  **Rotazione crediti piano free** (500 crediti/mese, reset il 1°):
  SPORTS_INTERVAL_DAYS calibrato su ~407/mese (top leghe ogni 2gg, coppe
  ogni 3gg, resto ogni 7/14/30gg) + **finestra QUERY_WINDOW_DAYS=7** (una
  chiamata copre l'intera settimana: nessuna partita persa anche con
  rotazioni rade) + **cap giornaliero DAILY_QUERY_BUDGET** (default 12,
  env `ODDS_DAILY_BUDGET`): le leghe in eccedenza sono rinviate al giorno
  dopo (log warning). Costo mensile verificato dal test
  test_budget_mensile_piano_free (<= 460). **Squadre fuori roster NON
  vengono piu' saltate**: `_match_team` ritorna il nome API e
  `expected_goals` usa il profilo di lega di default (i rating reali
  arrivano coi risultati). Chiave Brasileirao corretta:
  `soccer_brazil_campeonato`. Test: test_odds_api.py.
- **Partite saltate MAI silenziose**: fetch_and_analyze_today traccia le
  partite trovate ma non analizzate → saltate.json + `/api/analisi` (campo
  `saltate`) + sezione nel report. Con la copertura mondiale il campo e'
  vuoto per design (ogni partita e' analizzata).
- **Webapp**: 8 sezioni live (Dashboard, Calcola, Schedina, Storico, Cassa,
  Calendario, Backtest, Value).
- **Dedup dataset ML** (02/09, audit hash 36aa024f): doppio livello —
  (1) `tracker` migra automaticamente i vincoli UNIQUE sui ledger ANCHE per
  DB nati prima (dedup normalizzato: "Over 2.5"=="over", "Inter"=="1" via
  home/away; backup _old mai perso, recupero automatico se la migrazione
  viene interrotta); (2) `ml_dataset.dedupe_training_rows` dedup con chiave
  normalizzata a livello pipeline (stessa scommessa da predictions+bets =
  1 riga) — idempotente per CLI/API/ensemble/audit. `ml_audit` usa la
  STESSA chiave (audit e pipeline concordano). VERIFICATO IN PRODUZIONE:
  autoindex UNIQUE attivi su predictions/bets, 0 duplicati, audit pulito.
- **CLV vig-free corretto** (02/09): `performance_report._clv_stats` ora
  USA davvero `clv_vig_free()` (devig) invece di duplicare il vs-Pinnacle,
  e le righe con UN SOLO campione prezzo (closing = eco del segnale, CLV
  finto 0) sono escluse dalle medie. Il CLV vig-free -3.85% del 01/09 era
  esattamente 1/1.04-1: artefatto del fallback overround stimato, NON un
  segnale di mercato. Il report mostra il conteggio "in attesa di chiusura".
- **Backtest & Monte Carlo** (`backtest_mc.py`, 02/09): walk-forward SENZA
  look-ahead (ensemble addestrato solo sulle giornate precedenti; XGBoost se
  disponibile, altrimenti LR) + staking con l'ADAPTIVE_STAKING di produzione
  + 1000 percorsi Monte Carlo: ROI base/mediana/p5-p95, MAX DRAWDOWN
  base/mediana/p95, P(riduzione), P(≥5 perdite di fila). CLI
  `venv/bin/python backtest_mc.py --formato` e comando `/backtest_mc [sims]`.
  Guardia: servono ≥10 righe chiuse (oggi 8: si attivera' da solo con il
  ledger che cresce — ritornare quando il ledger ha 15+ chiusure).
- **Alert crollo quota** (rlm_alert.py, 02/09): oltre a RLM/steam, nuovo
  trigger URGENTE "CROLLO QUOTA" (calo ≥5% dal primo snapshot, basta 1
  aggiornamento = 2 snapshot per la velocità). Job già attivo ogni 5'
  14:00–23:50 ITA; destinatari: admin + iscritti, cooldown 60'/match.
- **Segnali mercato nel report + webapp** (market_signals.py, 02/09):
  aggregatore condiviso che classifica i segnali value attivi con i VERI
  rilevatori (line_movement + rlm_alert: steam/crollo/RLM, niente più proxy
  euristici). Esposto in: sezione "Line Movement" di `format_daily_report`,
  `GET /api/market_signals` (summary + signals ordinati per severità) e
  pagina webapp `/movimenti` (badge per tipo + card per segnale). CLI
  `venv/bin/python market_signals.py [--json]`. Fix build webapp: rimossa
  chiamata morta `proStake` in schedina/page.tsx (rompeva `npm run build`).
- **Backup centralizzato** (`backup_manager.py`, 02/09): snapshot
  data/backups/<ts>/ con DB (SQLite backup API) + INTEGRITY CHECK
  (PRAGMA quick_check) + dataset ML RIGENERATO (csv+json, sempre fresco e
  già deduplicato) + copia data/. Rotazione BACKUP_KEEP (env, default 7),
  timestamp con microsecondi. Usato da backup_data_job (03:30 UTC + avvio)
  e comando `/backup` (solo admin). VERIFICATO IN PRODUZIONE: integrity ok,
  56 CLV, dataset ML, 78 file data/.
- **Test**: 529 test verdi (la suite completa richiede ~8 min).

## Moduli avanzati (Settembre 2026)

- **ML Ensemble** (`ml_ensemble.py`): Logistic Regression numpy-only che
  combina le probabilità Poisson con un classificatore addestrato sul
  dataset storico. Peso dinamico basato sul Brier score. Save/load in
  `data/ensemble_model.json`. Integrato in `fixture_engine._analyze_match`.
- **Calibrazione isotonica** (`probability_calibration.py`, 04/09): PAVA
  numpy-only che corregge l'overconfidence di XGBoost/LR mappando gli
  score sulle frequenze empiriche. Fit su split out-of-sample (mai sui
  dati di training), attiva con ≥60 campioni chiusi, metriche
  Brier/ECE pre-post nel report. Integrata in `ml_ensemble.py`
  (train/predict/save/load, flag `calibrated`).
- **Line Movement Tracking** (`line_movement.py`): tabella `price_snapshots`
  registra i prezzi ad ogni analisi. RLM detection (reverse line movement =
  segnale sharp money quando il prezzo si muove contro il pubblico) e steam
  move detection (movimento > 6% in < 30 min). CLI per analisi.
- **Bookmaker Advantage** (`bookmaker_advantage.py`): confronta quote Pinnacle
  (sharp) con i soft book. Rileva lag (soft book non aggiornato) e calcola
  l'edge aggiuntivo dal lag. Integra in `fixture_engine`.
- **Adaptive Staking** (`adaptive_staking.py`): Kelly frazionato dinamico
  (0.10-0.35) con confidence weighting (market_edge, ML confidence, CLV,
  status) e drawdown protection (>10% → riduzione stakes). Integrato in
  `auto_bet.py` (ogni puntata ha stake diverso).
- **Dynamic Blend** (`market_calib.py`): `blend_probability()` ora accetta
  `league`, `odds`, `model_samples` per calcolare il peso dinamico.
  `LEAGUE_EFFICIENCY` con score per 30+ leghe (Premier League 0.85 →
  Indian Super League 0.35). Mercato efficiente → peso modello basso.
- **CLV Vig-Free** (`market_calib.py`): `clv_vig_free()` calcola CLV sulla
  closing line devigata (non la quota grezza). Corregge la sovrastima del
  CLV tradizionale. Il report mostra CLV raw, vig-free e vs Pinnacle.
- **Market Diagnose** (`market_diagnose.py`): diagnosi calibrazione per
  mercato. Confronta ROI realizzato vs EV atteso, identifica mercati
  critici (gap >= 3pp) e suggerisce tuning (blend, devig, soglia EV).
- **Fix Timezone Job**: tutti i job Telegram ora usano `IT_OFFSET=2` per
  convertire UTC → ora italiana. Prima il report delle 23:50 partiva
  alle 01:50 italiane!
- **Dedup dataset ML** (02/09, audit hash 36aa024f): doppio livello —
  (1) `tracker` migra automaticamente i vincoli UNIQUE sui ledger ANCHE per
  DB nati prima (dedup normalizzato: "Over 2.5"=="over", "Inter"=="1" via
  home/away; backup _old mai perso, recupero automatico se la migrazione
  viene interrotta); (2) `ml_dataset.dedupe_training_rows` dedup con chiave
  normalizzata a livello pipeline (stessa scommessa da predictions+bets =
  1 riga) — idempotente per CLI/API/ensemble/audit. `ml_audit` usa la
  STESSA chiave (audit e pipeline concordano). VERIFICATO IN PRODUZIONE:
  autoindex UNIQUE attivi su predictions/bets, 0 duplicati, audit pulito.
- **CLV vig-free corretto** (02/09): `performance_report._clv_stats` ora
  USA davvero `clv_vig_free()` (devig) invece di duplicare il vs-Pinnacle,
  e le righe con UN SOLO campione prezzo (closing = eco del segnale, CLV
  finto 0) sono escluse dalle medie. Il CLV vig-free -3.85% del 01/09 era
  esattamente 1/1.04-1: artefatto del fallback overround stimato, NON un
  segnale di mercato. Il report mostra il conteggio "in attesa di chiusura".
- **Backtest & Monte Carlo** (`backtest_mc.py`, 02/09): walk-forward SENZA
  look-ahead (ensemble addestrato solo sulle giornate precedenti; XGBoost se
  disponibile, altrimenti LR) + staking con l'ADAPTIVE_STAKING di produzione
  + 1000 percorsi Monte Carlo: ROI base/mediana/p5-p95, MAX DRAWDOWN
  base/mediana/p95, P(riduzione), P(≥5 perdite di fila). CLI
  `venv/bin/python backtest_mc.py --formato` e comando `/backtest_mc [sims]`.
  Guardia: servono ≥10 righe chiuse (oggi 8: si attivera' da solo con il
  ledger che cresce — ritornare quando il ledger ha 15+ chiusure).
- **Alert crollo quota** (rlm_alert.py, 02/09): oltre a RLM/steam, nuovo
  trigger URGENTE "CROLLO QUOTA" (calo ≥5% dal primo snapshot, basta 1
  aggiornamento = 2 snapshot per la velocità). Job già attivo ogni 5'
  14:00–23:50 ITA; destinatari: admin + iscritti, cooldown 60'/match.
- **Segnali mercato nel report + webapp** (market_signals.py, 02/09):
  aggregatore condiviso che classifica i segnali value attivi con i VERI
  rilevatori (line_movement + rlm_alert: steam/crollo/RLM, niente più proxy
  euristici). Esposto in: sezione "Line Movement" di `format_daily_report`,
  `GET /api/market_signals` (summary + signals ordinati per severità) e
  pagina webapp `/movimenti` (badge per tipo + card per segnale). CLI
  `venv/bin/python market_signals.py [--json]`. Fix build webapp: rimossa
  chiamata morta `proStake` in schedina/page.tsx (rompeva `npm run build`).
- **Backup centralizzato** (`backup_manager.py`, 02/09): snapshot
  data/backups/<ts>/ con DB (SQLite backup API) + INTEGRITY CHECK
  (PRAGMA quick_check) + dataset ML RIGENERATO (csv+json, sempre fresco e
  già deduplicato) + copia data/. Rotazione BACKUP_KEEP (env, default 7),
  timestamp con microsecondi. Usato da backup_data_job (03:30 UTC + avvio)
  e comando `/backup` (solo admin). VERIFICATO IN PRODUZIONE: integrity ok,
  56 CLV, dataset ML, 78 file data/.
- **Surebet engine indipendente** (`surebet_engine.py`, 05/09): scanner di
  arbitraggio su mercati h2h a 2 esiti per NBA (`basketball_nba`), MLB
  (`baseball_mlb`) e Tennis (chiavi per torneo `tennis_*`, configurabili via
  `SUREBET_SPORTS`). Default CREDITO-CONSERVATIVO: solo NBA+MLB con TTL 6h
  (~8 crediti/giorno, ~240/mese — la chiave e' CONDIVISA col calendario
  value che ne usa ~407-460 su 500 del piano free: NON aggiungere tornei
  tennis se i crediti residui sono bassi). Trigger matematico (1/A)+(1/B)<1 su coppie di bookmaker
  con ALMENO un SOFT (`SUREBET_SOFT_BOOKS`: Snai, GoldBet, Bet365, William
  Hill, Bwin, Unibet, Sisal, Eurobet, Betflag, Novibet, Stanleybet, 888,
  Marathonbet, 10bet, Betway, Paddy Power, Coral, betsson) contro SHARP
  (`SUREBET_SHARP_BOOKS`, default Pinnacle) o soft-vs-soft. Stake esatti
  proporzionali agli inversi su `SUREBET_BUDGET` (default €100), profit/ROI
  netto garantito. **INDIPENDENZA totale dal bot Value Bet**: cache propria
  (data/surebet/cache), log JSONL proprio (data/surebet/opportunities.jsonl,
  dedup 24h), nessun import da tracker/bot (test dedicato lo verifica),
  loop separato `venv/bin/python surebet_engine.py --loop N` (non toccare
  run_all.py: il bot resta sul volume unico). Delivery: Telegram con formato
  dedicato (ROI, evento, quote, stake per bookmaker) via POST diretto
  all'API Telegram + webhook n8n già predisposto (`SUREBET_WEBHOOK_URL`,
  payload JSON via `build_json_payload`). Crediti: TTL 1h per sport, stop
  sotto `SUREBET_MIN_REMAINING` (default 50; il piano free ~500/mese è già
  quasi tutto consumato dal calendario value → monitorare crediti).
  Tripwire: nessun riferimento al vecchio exchange nel codice del modulo.
  **COLLAUDO REALE 05/09 su Railway** (chiave API vera): scansione NBA → 41
  match, 39 con h2h a 2 esiti, **3 surebet reali trovate** (es. Rockets vs
  Mavericks: Dallas @4.25 1xBet vs Houston @1.34 Nordic Bet, ROI +1.88%).
  NESSUNA Pinnacle/sharp per NBA in eu/uk (tutte le coppie sono soft-soft):
  la copertura sharp dipende dallo sport — verificare per MLB/tennis.
  Fix delivery: parse_mode HTML (Markdown legacy dava 400 su nomi reali).
  Fix anti-garbage 05/09: filtro `SUREBET_MAX_ODDS` (default 30) PRIMA del
  trigger matematico — coppie con quote sporche/mercati illiquidi (es. reale
  Arizona @85.00 vs @1.03: (1/85+1/1.03)<1 scatta per artefatto aritmetico)
  scartate da `is_sane_odds()`. Test dedicati (TestMaxOddsFilter).
  **VERIFICA CRON IN PRODUZIONE (05/09)**: il cron */15 gira davvero. I log
  delle esecuzioni dei cron job Railway NON sono esposti dal CLI (si vede
  solo "Mounting volume/Starting Container") → il "0 log" era un falso
  allarme. Cattura live via `railway ssh -s surebet` durante un run (con
  hold temporaneo): heartbeat.json scritto da pid 1, cache MLB aggiornata
  (remaining 263, 15 match) e 69 opportunita' gia' loggate sul volume.
  Aggiunti: heartbeat.json a ogni run (ts/sports/pid), log INFO
  avvio/completamento, hold opzionale `SUREBET_CRON_HOLD_SECONDS` (default
  0, usato solo per diagnosi) per tenere il container attivo qualche
  secondo ed ssh-are durante il run. Config attiva 05/09: MLB-only (NBA
  off-season), TTL 6h, cron */15.
- **Test**: 547+ test verdi (la suite completa richiede ~9 min).
- **Sicurezza**: rotazioni token 01/09, 02/09 e **04/09** verificate
  (Telegram `@Calcifrrbot`, ID 8372645521). Rotazione 04/09 completata
  con Opzione A: token esposto in chat REVOCATO (getMe col vecchio → 401)
  e nuovo token attivo (getMe 200), letto dalle env Railway e portato nel
  vault cifrato locale SENZA mai apparire in chat (46 chars, diff vs
  vecchio verificata). Tripwire `test_secret_hygiene.py` rende permanente
  il vincolo "nessuna credenziale in chiaro nel codice" (vedi regola 7).

## Prossimi passi possibili (non urgenti)

- ExecutionEngine: ottenere le credenziali (SX Bet: `SX_API_KEY`/`SX_PRIVATE_KEY`
  + proxy wallet deployato e finanziato su SX Rollup; Smarkets:
  `SMARKETS_USERNAME`/`SMARKETS_PASSWORD`; aggregatore BetInAsia BLACK /
  MollyBet: `EXECUTION_APP_KEY/USERNAME/PASSWORD`), caricarle nelle env
  Railway + vault locale e fare le prime chiamate reali:
  `venv/bin/python execution_engine.py --provider sxbet --probe
  --market <marketHash_hex> --selection 1` (o `--provider smarkets`, o senza
  `--provider` per l'aggregatore) con stake 1€ per misurare latenza/slippage
  veri prima di passare a stake reali. ⚠️ Da rete italiana `api.smarkets.com`
  è inibito (ADM): il probe Smarkets va eseguito da Railway/rete estera;
  `api.sx.bet` è raggiungibile. Wiring auto_bet → execution_engine
  completato l'08/09 (vedi Stato attuale): resta il collaudo live in
  produzione (AUTO_BET_MODE=live + EXECUTION_PROVIDER=sxbet su Railway).
- Surebet engine: schedulare il loop in produzione (crontab/cron Railway o
  secondo servizio) e monitorare i crediti the-odds-api (il piano free è
  quasi saturo col calendario value). Verificare su dati reali quali
  bookmaker soft the-odds-api copre davvero per NBA/MLB/Tennis (il match
  per sottostringa è estensibile via env).
- Quando il ledger avrà 100+ previsioni chiuse: usare `market_diagnose.py`
  per identificare mercati critici e ajustare blend/devig/soglie.
- Eseguire `/backtest_mc` con 15+ previsioni chiuse (oggi 8): le metriche
  Monte Carlo (MaxDD p95) diventano significative solo con abbastanza dati.
✅ Segnali RLM/steam/crollo nel report + webapp (02/09, market_signals.py).
- Integrazione XGBoost quando il dataset ML raggiunge 500+ campioni
  (attualmente Logistic Regression numpy-only per evitare deps pesanti).
- Cambiare `IT_OFFSET` da 2 a 1 a fine ottobre (ora legale invernale).

### Miglioramenti 10/09/2026

- **Credit watchdog** (`bot.py`): job ogni 6h che legge le cache
  `toa_*.json` e invia alert Telegram sotto le soglie
  (50→warning, 20→alert, 10→danger, 5→critical). Zero costo API.
  Registrato in `main()` con `run_repeating`.
- **Settlement watchdog**: timeout per puntate LIVE >6h senza
  settlement — avviso automatico se una bet `mode='live'` resta
  aperta senza `esito_finale` per oltre 6 ore.
- **GET /api/credits** (`web_api.py`): endpoint REST che restituisce
  remaining_min, sport_cached, days_to_reset, dettagli per sport,
  soglie di allarme e stato operatività.
- **Filtro proattivo crediti** (`odds_api.py`): `should_query_sport()`
  e `get_remaining()` — disattiva automaticamente le leghe a basso
  valore quando i crediti scendono sotto 50/30/15:
  - >=50: tutto attivo
  - <50: solo leghe core (intervallo ≤7gg)
  - <30: solo top 6 leghe
  - <15: solo Serie A, PL, La Liga
- **Tennis validation gate** (`tennis_sandbox.py`): `MIN_OBSERVATIONS=10`
  — se il ledger ha meno di 10 osservazioni, gli stake vengono
   ridotti al 50% e viene registrato un avviso (`is_validated()`).
   Previene scommesse su dati statisticamente insufficienti.
- **ALTER TABLE signals surface** (`tracker.py`): migration idempotente
  che aggiunge la colonna `surface` alla tabella `signals`.
  `log_signal` ora accetta il parametro `surface`.
- **Railway Agent setup**: skills `use-railway` e MCP server
  installati per Claude Code, OpenCode, GitHub Copilot.

### Strategia: aumento frequenza scommesse (10/09/2026)

**Obiettivo**: aumentare il numero di puntate giornaliere abbassando le soglie di qualificazione.

**Modifiche implementate:**
- `MARKET_EDGE_MIN` da 0.03 a **0.02** (+2pp invece di +3pp)
- `EV_MIN` da 0.03 a **0.02** (+2% invece di +3%)
- Nuovo tier **moderate** tra `value` e `strong_value`
  - strong_value: edge >= 5pp (cap 10% bankroll)
  - value: edge >= 2pp (cap 7% bankroll)
  - moderate: edge >= 0pp / EV >= 2% (cap 4% bankroll)
- `confidence_kelly_fraction` esteso con tier moderate (+0.1 score)
- `adaptive_stake` con cap ridotto per moderate (4% fisso)
- `_today_value_picks` include `moderate` nei candidati
- `get_signal_tier()`: nuova funzione di classificazione
- `filter_value_bets`: ora assegna `tier` a ogni segnale
- **MIN_STAKE_EUR**: 1.0 -> **0.01** (rimosso floor 2 EUR, minimo exchange USDC)
- **Notifica FULLY_FILLED** (`bot.py`): dopo ogni giro auto_bet,
  per ogni ordine `status == "FULLY_FILLED"` e `mode == "live"`
  viene inviato un messaggio Telegram in tempo reale con
  partita, esito, quota, stake e bet_id.

**Effetto atteso**: circa **2-3x piu' pick qualificati** (da ~5/giorno a ~10-15/giorno).
Il tier moderate ha cap ridotto (4%) per contenere il rischio sui segnali deboli.
Il sistema mantiene il fail-safe: ogni segnale deve comunque battere il mercato
di almeno 2pp e avere EV >= 2%.

**Notifiche**: l'admin riceve un messaggio Telegram ogni volta che un
ordine LIVE viene FULLY_FILLED, con tutti i dettagli (match, esito,
quota, stake, bet_id).

### Cambio strategia: SOLO CAMPIONATI VINCENTI (12/09/2026)

**Motivazione**: la strategia "solo favoriti 1.30-1.80" (11/09)
ha dato CLV vig-free **-3.3%** e circa 1 segnale/settimana
(molto poco). Il backtest storico rivela differenze massive tra
campionati:
- **Vince**: Bundesliga (+28.3%), PL (+16.2%), Turchia (+22.1%),
  Ligue 1 (+9.1%)
- **Perde**: Serie A (-5.9%), La Liga (-6.3%), Grecia (-69.4%)

**Implementazione** (value_filter.py):
- Fascia quote **1.50-2.20** (esclude il "pantano" 1.30-1.45
  con ROI -9.9%)
- `STRATEGY_LEAGUES`: solo campionati con ROI positivo
  (PL, Bundesliga, Turchia, Ligue 1, Eredivisie)
- Edge differenziato: PL/BL +2pp, Turchia/Ligue1 +2.5pp
- Kelly adattivo: PL 1.2x, BL 1.3x, altri base 1.0x
- Cap stake: PL/BL 2%, Turchia/Ligue1 1.8%, altri 0.5%
- Fallback severo per leghe non elencate (effectivamente bandite)

**Test**: `TestStrategiaPerLega` in `test_value_filter.py`
(10 test: leghe vincenti/perdenti/desconosciute, is_sane per
lega, get_league_strategy). Tripwire: nessun segnale da Serie A/
La Liga/Grecia.

### Cambio di strategia: STOP + SOLO FAVORITI NETTI (11/09/2026)

**Direttiva del proprietario**: fermare subito le puntate e vietare
**tassativamente** le scommesse su squadre sfavorite/quote alte (anche
sotto a 3.0), riprogettando la logica verso i soli **favoriti netti**
per evitare il rischio di bancarotta.

**1) STOP IMMEDIATO (produzione, verificato sul container)**
- `data/execution/auto_bet_mode.json` = `{"mode": "off"}` sul volume
  Railway → `kill_switch_status()` = {override: off, env_mode: live,
  effective: off, provider_ready: true}; `_execution_mode()` = "off".
  Nessun ordine reale né simulato finché non si usa `/autobet live`.
- **PAUSA SETTLEMENT** (nuovo, `tracker.py`): flag persistente
  `data/execution/settlement_paused.json` (+ env `SETTLEMENT_PAUSED=1`)
  letto da `settlement_paused()`. Con la pausa attiva `settle_bets`,
  `settle_predictions`, `settle_cassa` e `settle_sx_bets` NON chiudono
  nulla e `bot._update_results` esce **prima** di `fetch_scores` (zero
  crediti the-odds-api). Nuovo comando admin
  **`/settlement [on|off|stato]`** per riattivare da Telegram.
  Il tennis sandbox (ledger paper separato) continua a girare: serve
  all'apprendimento ELO e non tocca il bankroll reale.

**2) NUOVO GATE: SOLO FAVORITI NETTI (`value_filter.py`)**
- `ODDS_MAX` da **3.00 → 1.80** (favorito forte, prob. implicita ~55%);
- nuovo `FAVOURITES_ONLY = True` + `MIN_FAVOURITE_MARKET_PROB = 0.50`:
  con prob. di mercato nota l'esito deve essere il **favorito** (≥ 50%);
- nuova `eligible_favourites(candidates)`: tiene solo i candidati con
  prob. di mercato **massima** del mercato, ≥ 50% e quota ≤ 1.80;
  `favourites_gate_reason()` fornisce il motivo standard.
- **Selezione del segnale cambiata**: il candidato giocabile non è più
  il max-EV assoluto ma il **miglior EV tra i favoriti netti**
  (`fixture_engine._analyze_match` e `sx_signals.scan`). Se nessun esito
  qualifica, il match è `rejected` e **non scrive nemmeno una riga** nel
  ledger `predictions`: gli esiti sfavoriti non esistono più per il
  sistema. Il CLV viene registrato solo per i favoriti (niente prezzi di
  esiti scartati nelle medie di chiusura).
- **Difesa in profondità** (`auto_bet._today_value_picks`): il cap quota
  e la prob. di mercato sono rifiltrati a valle, così eventuali righe
  storiche (scritte prima dell'11/09) non possono mai diventare ordini.
- Tier/EV/edge minimi **invariati** (EV ≥ 2%, edge ≥ 2pp, tier
  value/strong_value/moderate come dal 10/09).

**3) Test**: nuovo `test_favourites_only.py` (tripwire di gate su
`value_filter`, `fixture_engine`, `sx_signals`, `auto_bet` — incluso il
caso "riga storica a quota 2.10 → nessun ordine") e nuovo
`test_settlement_pause.py` (flag, env, i tre settle bloccati, riattivazione
che salda ancora, `_update_results` che non scarica risultati in pausa).
`test_value_filter`/`test_market_calib`/`test_sx_signals`/`test_auto_bet*`
aggiornati ai favoriti netti (quote ≤ 1.80, prob. di mercato ≥ 50%).

**Numeri del ledger al momento della decisione** (chiusure per fascia
quota, campioni piccoli): 1.50-1.80 +0.107 (3 chiuse), 1.80-2.00 −0.213
(5), 2.00-2.50 +0.327 (19), 2.50-3.00 −0.196 (20), ≥3.00 −0.158 (22).
⚠️ Nota tecnica: con quote più corte il Kelly calcola frazioni MAGGIORI,
quindi lo stake per singola bet sale.

**4) CAP STAKE SEVERO (stessa direttiva)**: `adaptive_staking` scende da
7%/10% a **1% del bankroll per value e moderate** e **2% per
strong_value** (default di codice; env Railway `STAKE_CAP_PCT=0.01` e
`STAKE_CAP_PCT_STRONG=0.02`, entrambe in `preserve()` in
`.railway/railway.ts`). Il vecchio cap fisso 4% dei moderate è rimosso:
i segnali deboli non possono valere più dei value. `value_filter.MAX_STAKE_PCT`
allineato a 1% (era 3%) così i tool (schedina/`/value`) mostrano lo stesso
cap che il bot applica.

**5) CAP SEVERO VINCOLANTE — `STAKE_CAP_HARD` (11/09, `auto_bet.py`)**:
prima il floor dell'exchange vinceva sul cap (`pick_stake = max(stake,
1.0)`) → con 38 USDC nel wallet ogni bet era 1 USDC = **2.6%**, non 1%.
Ora, con `STAKE_CAP_HARD` attivo (**default**), una bet il cui stake
cappato è sotto il minimo ordine viene **SALTATA (fail-closed)**, sia in
FASE 1 (Kelly/cap) sia dopo i risk cap (correlazione 30% / esposizione
totale 40%). Conseguenza operativa da conoscere: con bankroll < 100 USDC
il cap 1% è sotto 1 USDC → **nessun ordine parte** finché il wallet non
cresce (≥ 100 USDC per il 1%, ≥ 50 USDC per il 2%) oppure finché non si
accetta il floor con `STAKE_CAP_HARD=0`. `/autobet` mostra il cap severo e
avvisa se il saldo attuale non lo sostiene. Env dichiarata in
`preserve()` in `.railway/railway.ts`. Tripwire:
`test_favourites_only.TestStakeCapSevero` (incl. wallet 38 USDC → zero
ordini) e `test_auto_bet_live` (hard ON = salta, OFF = floor 1 USDC).

### Fix strutturali critici 11/09/2026 (mapping leghe + guardia cassa)

**1) FIX MAPPING LEGHE (`sx_signals.py`) — il bug che bloccava il
settlement e lasciava le bet aperte.** Il fuzzy `SequenceMatcher >= 0.55`
restituiva SEMPRE il nome più simile, anche quando era sbagliato. Sample
reale delle etichette SX (API pubblica, ~40 label) con i mapping vecchi:
`Major League Soccer -> League One`, `German Bundesliga -> Austrian
Bundesliga`, `Jupiler League -> Premier League`, `K1-League -> J1 League`,
`LigaPro -> 3. Liga`, `Primera Nacional -> Primeira Liga`, `Primera A ->
Primeira Liga`, `Primera Division -> Primeira Liga`, `First League ->
A-League`, `K2-League -> K League 1`. Conseguenza: il settlement
interrogava the-odds-api sulla competizione SBAGLIATA, non trovava mai il
punteggio e le bet `sx-*` restavano aperte per sempre.
- Nuova tabella `SX_LEAGUE_ALIASES` DETERMINISTICA (etichetta SX → chiave
  `SPORTS_MAP`, con `None` = "competizione non coperta": mai indovinare).
  Copre le 39 label reali osservate (`MLS`, `Liga Profesional -> Argentina
  Primera`, `Jupiler League -> Belgian First Div`, `K1-League -> K League
  1`, `The Championship -> EFL Championship`, `Superliga -> Superliga
  Danimarca`, `Europa League_UEFA -> Europa League`, ecc.).
- Risoluzione a 3 stadi in `_league_sx_to_sports_map`: alias → chiave
  `SPORTS_MAP` esatta → fuzzy STRETTO (`_strict_fuzzy_league`: match exact
  1.0, prefisso/suffisso di paese 0.97, altrimenti similarità ≥ 0.92 o
  token contenuti, con guardia di ambiguità ≥ 0.08 sul secondo). Esiti
  ambigui (`Serie B` Italia vs Brasile, `Super League`) → `None`.
- Nuovo resolver `league_to_sport(league)`: chiave `SPORTS_MAP`, etichetta
  SX grezza o alias → sport key. Usato dal settlement (`sx_signals` e
  `bot._update_results`), che ora LOGGA le leghe non mappate invece di
  saltarle in silenzio.
- `_results_from_the_odds_api` raggruppa i match per sport key RISOLTO e
  matcha i nomi sia stretti sia loose (`_loose_team`): 'FC Cincinnati'
  aggancia 'Cincinnati' senza invertire casa/trasferta.
- `repair_sx_leagues()` + CLI `venv/bin/python sx_signals.py repair`:
  rilegge i mercati attivi da SX (API pubblica, zero crediti) e riscrive
  `matches.league` delle partite `sx-*`, recuperando le righe salvate col
  fuzzy vecchio (il `scan()` ogni 15' fa già self-heal delle partite in
  finestra, perché `save_match` è INSERT OR REPLACE).
- Tripwire: `test_league_mapping.py` (alias reali, rifiuti espliciti,
  fuzzy stretto, resolver, settlement con lega alias e con lega non
  mappata → 0 chiamate API + bet aperta, repair).

**2) GUARDIA CASSA con finestra temporale (`tracker.settle_cassa`).**
La cassa aggancia le bet ai risultati per NOME (non per id): la stessa
coppia di squadre può comparire più volte (stagione precedente,
andata/ritorno) e il vecchio codice prendeva la riga "più recente" senza
limite. Ora:
- ogni candidato per coppia normalizzata è raccolto (non solo il più
  recente) e la scelta avviene per **vicinanza temporale** alla data della
  bet (`data`, in fallback `timestamp`);
- il risultato viene usato solo entro `CASSA_MATCH_WINDOW_DAYS` (default
  **14**, env) dalla bet; fuori finestra la riga **resta in gioco** e
  viene loggata (`out_window`);
- fallback storico (il più recente) SOLO se non esiste alcuna data
  utilizzabile.
- Tripwire: `test_cassa_window.py` (risultato vecchio bloccato, scelta del
  più vicino, env override, fallback senza date, idempotenza).

**3) BUG INCIDENTALE TROVATO E FIXATO — `STALE_INPLAY_HOURS` non
definita (`odds_api.py`).** `_cache_is_stale_for_settlement` usava la
costante ma non era mai stata definita → `NameError` inghiottito
dall'`except` → la cache punteggi "in corso da ore ma completed=False"
veniva considerata valida e il settlement restava bloccato (un'altra
causa di bet aperte). Aggiunta `STALE_INPLAY_HOURS = 3` (un match di
calcio finisce entro ~2h). `test_scores_cache_stale` torna verde.

**4) `backtest_mc` minimo stake allineato alla produzione.**
`_simulate_sequence` scartava gli stake `< 2.0` (minimo Exchange Italia);
con il cap severo 1% su bankroll 100 lo stake è 1.0 (floor SX) → TUTTE le
bet venivano scartate e la simulazione dava 0. Nuova `MIN_STAKE` (env
`MIN_STAKE_EUR`, default 1.0). `test_backtest_mc` torna verde.

**5) SIMULAZIONE STAKE NEL BACKTEST (`historical_backtest.py`).** Nuovo
flag `--production`: applica la strategia di produzione (gate
`is_sane(favourites_only=True)`, `--max-odds` default 1.80, `--no-ou`
implicito) e usa lo stake Kelly 1/4 con cap 1% di `value_filter.kelly_euro`.
Risultati sul dataset cached (16.273 partite, `--no-ensemble`):
- **modalità PRODUZIONE: 0 bet** — nel mercato storico (quote di
  chiusura/Pinnacle) il gate favoriti + EV ≥ 2% non trova alcun valore: il
  blend pesa il mercato e sui favoriti il modello NON batte la closing
  line. Conferma il motivo dei pochi segnali live.
- baseline di ricerca (universo `MAX_ODDS` 5.0): 7.861 bet, ROI −8.27%,
  MaxDD 99.8% (bankroll 100 → 3.08); `by_market` OU −6.85%, 1X2 −14.73%.
- 1X2-only (`--no-ou`) flat: 2.619 bet, ROI −9.95%, hit 27.8%, avg quota
  3.41 (universo di ricerca, NON il gate favoriti).

Stato test: tutti i file verdi (inclusi i 4 nuovi/aggiornati); la suite
completa resta lunga (>10 min) → girare a file/capitoli.

### Guardrail di rischio 11/09/2026 (quota minima, edge, stop-loss, liquidità)

Direttiva del proprietario: rendere il sistema più prudente sui favoriti.
Quattro protezioni aggiunte, tutte con env override (nessun redeploy di
codice) e tripwire dedicati in `test_risk_guards.py`.

**1) QUOTA MINIMA `ODDS_MIN` 1.50 → 1.30 (`value_filter.py`).**
Insieme a `ODDS_MAX` (1.80) definisce la fascia dei favoriti netti
**1.30–1.80**: sotto 1.30 il ritorno per unità di stake non compensa il
rischio (probabilità implicita > 77%). Sotto soglia `is_sane` rifiuta con
"quota troppo bassa". Il messaggio di `favourites_gate_reason()` e il
display webapp (`webapp/app/value/page.tsx`) sono allineati.

**2) EDGE MINIMO +3pp (`market_calib.py`).** `MARKET_EDGE_MIN` e
`MARKET_EDGE_MODERATE` riportati da 0.02 a **0.03** (+3pp di probabilità
stimata vs mercato devigato): il bot NON punta il favorito "alla cieca",
deve battere il mercato. `MARKET_EDGE_STRONG` resta +5pp per i
`strong_value`. ⚠️ Contro la direttiva del 10/09 (che li aveva abbassati a
+2pp per aumentare la frequenza): ora la priorità è la prudenza, non il
volume. `EV_MIN` resta 2%.

**3) STOP-LOSS GIORNALIERO (`auto_bet.py`).** Se il bankroll scende del
**5%** (`DAILY_STOP_LOSS_PCT`) rispetto al valore registrato a inizio
giornata, le puntate vengono **bloccate per 24h** (`DAILY_STOP_HOURS`).
- Stato persistente sul volume (`data/execution/daily_stop.json`, scrittura
  atomica): sopravvive ai redeploy, si ri-arma al primo giro del giorno
  successivo.
- In LIVE il bankroll è il saldo REALE del wallet SX (rischio = denaro
  vero); in SIM la cassa. Fail-open: un file corrotto NON blocca le
  puntate, ma un blocco attivo resta rispettato.
- Hook in `run_today_bets` (check subito dopo la determinazione del
  bankroll) + `daily_stop_status()` per `/autobet` + alert Telegram
  una-volta-al-giorno nel job (chiave `DAILY_STOP`, anti-spam col giro ogni
  60s) + `clear_daily_stop()` per la rimozione manuale.

**4) FILTRO LIQUIDITÀ SX (`sx_signals.py` + `auto_bet.py`).** Su un
exchange la quota mostrata può non essere disponibile: due guardie.
- **Generazione segnali** (`sx_signals.scan`): `SX_MIN_DEPTH_USDC`
  (default 15) sulla profondità totale del match e
  `SX_MIN_LEG_DEPTH_USDC` (default 5) sulla profondità minima del singolo
  esito → i mercati sottili non generano segnali.
- **Prima dell'ordine** (`auto_bet._live_available_size`): la size BACK
  disponibile al floor deve coprire lo stake, altrimenti l'ordine viene
  saltato (rischio slippage/riempimento parziale). Se il book non è
  leggibile o è in formato ignoto NON blocca (fail-open sulla lettura,
  fail-closed sulla size reale).

Stato test: `test_risk_guards.py` (nuovo) + `test_value_filter`,
`test_favourites_only`, `test_auto_bet*`, `test_sx_signals`,
`test_market_calib` tutti verdi.

### Verifica forzata dei guardrail + monitor scarti liquidita' (11/09/2026)

**1) VERIFICA FORZATA (`verify_guardrails.py`).** Script di diagnostica che
dimostra — con i log REALI e senza toccare la produzione (DATA_DIR e DB
temporanei, mai un provider reale) — che i guardrail bloccano davvero le
puntate. Esito ultimo giro: **tutti i guardrail BLOCCANO** (A-E).
- **A. Kill-switch OFF** → `modalita' 'off' (kill-switch o fail-closed):
  nessuna puntata`, 0 ordini.
- **B. Stop-loss -5%** → `STOP-LOSS GIORNALIERO — perdita 6.0% (>= 5%):
  puntate bloccate fino a ...`, 0 ordini anche se il bankroll risale.
- **C. Cap severo 1%** → con wallet 38 USDC lo stake Kelly×cap = 0.38 <
  minimo ordine 1.0: `CAP SEVERO: stake cappato 0.38 USDC < minimo ordine
  1.00 USDC ... ordine saltato (fail-closed)`, 0 ordini inviati al
  provider. Controprova con `STAKE_CAP_HARD=0`: il floor viene accettato
  e l'ordine parte (1 USDC).
- **D. Quote 1.30-1.80** → quota 1.90 scartata (`quota > 1.80`), quota
  1.20 scartata (`quota < 1.30: fascia favoriti 1.30-1.80`, gate aggiunto
  come difesa in profondita' in `auto_bet._today_value_picks`), esito non
  favorito scartato (`prob. mercato < 0.50`).
- **E. Liquidita' SX** → con stake 5 USDC i richiesti sono
  `max(5 × 2.0, 25.0) = 25.0` USDC al floor: book da 4 USDC →
  `liquidita' 4.00 < richiesta 25.00 ... salto (rischio slippage)`, 0 ordini
  inviati e scarto `order/depth_vs_stake` nel monitor; controprova con book
  da 26 USDC → l'ordine parte (1 chiamata al provider).
Uso: `venv/bin/python verify_guardrails.py` (exit 0 = tutto bloccato).

**2) MONITOR SCARTI LIQUIDITA' (`liquidity_monitor.py`, nuovo).**
Su SX Bet (exchange) uno scarto per book sottile e' un'edge potenzialmente
persa: il monitor la rende misurabile invece che silenziosa.
- **Log JSONL sul volume** (`data/execution/liquidity_skips.jsonl`, path
  overridabile con `LIQUIDITY_SKIP_LOG`). Tre tipi di evento:
  `scan` (segnale non generato: `sx_signals.scan`), `order` (ordine reale
  saltato: `auto_bet._live_fill`) e `partial` (riempimento parziale
  materializzato). Per gli scarti d'ordine registra anche l'**edge NON
  realizzato** (`EV × stake`), la metrica che quantifica il costo.
- **Bot**: job `liquidity_monitor_job` ogni 6h che logga SEMPRE lo stato e
  allerta admin+iscritti (anti-spam 1/giorno, chiave `LIQ_SKIP`) solo se ci
  sono scarti nelle 24h; sezione `💧 Scarti liquidita' SX` nel report
  giornaliero. Zero costi API (legge solo il JSONL).
- **CLI**: `venv/bin/python liquidity_monitor.py [--days N] [--json]`.
- Fail-safe totale: `record_skip`/`iter_events` non propagano mai eccezioni
  (un log corrotto o non scrivibile non blocca mai il giro puntate).
- Test: `test_liquidity_monitor.py` (12+ test: riepilogo/finestra/righe
  corrotte/fail-safe/scan/order/partial/report/tripwire job/difesa
  quota-min a valle).

Stato test: focus verdi — `test_liquidity_monitor`, `test_risk_guards`,
`test_bot`, `test_sx_signals`, `test_auto_bet_live`, `test_reports`,
`test_web_api`, `test_tier`, `test_performance_report`.

### Taratura soglie di liquidita' SX (11/09/2026, secondo giro)

Obiettivo del proprietario: il bot deve **rifiutare da solo** gli ordini su
mercati SX Bet senza liquidita' sufficiente a eseguire la quota richiesta
senza slippage. La pagina visiva degli scarti arrivera' dopo: qui si sono
tarate le soglie (tutte da env, zero redeploy di codice).

**1) GUARDRAIL D'ORDINE (`auto_bet.py`) — la parte vincolante.**
Prima bastava `profondita' al floor >= stake` (copertura secca: lo stake
poteva esaurire il lato del book). Ora il vincolo e' **copertura con
margine**:

```
richiesto = max(stake × SX_DEPTH_MULTIPLIER (2.0), SX_MIN_EXEC_DEPTH_USDC (25.0))
```

- `SX_DEPTH_MULTIPLIER` (default **2.0**): lo stake non deve consumare piu'
  della meta' del lato visibile al floor — altrimenti il resto della size
  muove il prezzo e la quota "richiesta" non e' piu' garantita.
- `SX_MIN_EXEC_DEPTH_USDC` (default **25.0**): soglia ASSOLUTA di libro al
  floor per qualunque stake (un mercato quasi vuoto non e' negoziabile
  nemmeno con 1 USDC).
- Nuova helper `auto_bet.required_depth(stake)` (unica fonte della formula,
  testata); il salto e' fail-closed e finisce nel monitor con
  `threshold = richiesto`, `extra.richiesto/multiplier/min_exec_depth`.
- Fail-open SOLO sulla lettura del book (formato ignoto/provider senza
  book): resta fail-closed sulla size reale misurata.
- Esempi: stake 1-5 USDC → servono 25 USDC al floor; stake 20 USDC → 40
  (il multiplo prevale: max(40, 25)).

**2) GUARDRAIL DI SCAN (`sx_signals.py`) — coerenza con l'ordine.**
- `SX_MIN_DEPTH_USDC` 15 → **25** (liquidita' TOTALE delle 3 leg: salute
  del mercato).
- `SX_MIN_LEG_DEPTH_USDC` resta **5** (ogni singolo esito esiste).
- NUOVO `SX_MIN_EXEC_DEPTH_USDC` (**25**, stessa env e stesso default di
  `auto_bet`): la **leg che verrebbe giocata** (il favorito scelto) deve
  avere almeno 25 USDC al floor. Sotto soglia il match non produce NESSUN
  segnale (`continue` prima di `save_match`): un segnale non eseguibile
  sarebbe rumore nel ledger e nel CLV. Scarto registrato come
  `scan/depth_exec_<esito>`.
- Effetto: le soglie di mercato restano morbide sulle leg NON giocate
  (l'underdog sottile non elimina piu' un match giocabile sul favorito),
  mentre la leg giocata e' allineata 1:1 al guardrail d'ordine.

**3) MONITOR (`liquidity_monitor.py`).** Default riallineati (25/5/25/×2.0),
nuovo blocco `thresholds` in `summary()` e riga "Soglie attive" nel report
Telegram; il messaggio di allerta finale elenca anche le nuove env.

**4) ENV in `preserve()`** (`.railway/railway.ts`): `SX_MIN_EXEC_DEPTH_USDC`
e `SX_DEPTH_MULTIPLIER` aggiunte accanto alle altre soglie liquidita', cosi'
`railway config apply` non le distrugge.

**Test**: `test_risk_guards.py` (soglie gemelle scan/ordine +
`required_depth` = max(multiplo, minimo), fallback su stake non numerico),
`test_liquidity_monitor.py` (book 6 USDC con stake 5 → salto per margine e
evento con `threshold`/`extra.richiesto`; scan con leg giocata a 6 USDC e
totale 86 → `depth_exec_1`), `verify_guardrails.py` (scenario E).
Tutti verdi nel focus: risk_guards + liquidity_monitor + sx_signals (39),
auto_bet* + favourites_only + value_filter + market_calib + tier + reports
(132), test_bot (20).

### Misura dell'impatto delle soglie (11/09/2026, `liquidity_impact.py`)

Prima di riattivare il bot: **quante puntate bloccano davvero le nuove
soglie?** Nuova diagnostica `liquidity_impact.py` (solo LETTURA: API
pubblica SX, zero crediti, zero ordini, nessuna scrittura sul ledger —
tripwire in `test_liquidity_impact.py`). Misura DUE grandezze che non vanno
confuse:
- **profondita' di mercato** di un esito = somma di TUTTI i livelli
  (quella usata dal filtro di `sx_signals.scan`);
- **size al floor** = somma delle size al prezzo migliore (quella usata dal
  guardrail d'ordine `auto_bet._live_available_size`).

CLI: `venv/bin/python liquidity_impact.py [--hours 72] [--max-events 200]
[--with-model] [--json] [--save file.json] [--from-cache file.json]`
(campione congelato in `data/execution/liquidity_impact_sample.json`).

**ISOLAMENTO DELLE VARIABILI (direttiva 11/09)**: il default misura
**SOLO la liquidita'**. Con `with_model=False` il modello non viene nemmeno
interrogato (`_row_for_event` esce prima di `expected_goals`): la selezione
del favorito dipende solo dal mercato (devig + fascia quota), quindi nessun
numero del conteggio scarti puo' essere inquinato da ratings/EV. Il gate
modello esiste come sezione **opt-in** (`--with-model`, variabile separata da
misurare quando i ratings saranno allineati). Tripwire:
`test_liquidity_impact` (default senza chiavi modello, modello mai chiamato,
verdetto solo-liquidita').

**Campione reale 11/09/2026 (197 partite, finestra 72h, tutte le leghe SX):**
- 177 partite coerenti (inv_sum 0.98-1.08).
- Filtro di MERCATO: **VECCHIE soglie 177/177 (100%), NUOVE 177/177 (100%),
  bloccate 0 (0.0%)**.
- 42 candidati favoriti (fascia 1.30-1.80 + prob. mercato): **42/42 (100%)
  eseguibili** col guardrail d'ordine.
- Sensibilita' allo stake (richiesto = max(stake×2, SX_MIN_EXEC_DEPTH_USDC)):
  stake 1-10 USDC **42/42**, stake 20 USDC 41/42 (97.6%).
- Se irrigidissimo la soglia della leg giocata: 25 USDC → 42/42, 50 → 41/42,
  100 → 38/42 (90.5%). La liquidita' dei 1X2 calcio su SX e' ABBONDANTE
  (size al floor: p10 102, p50 263, p90 986 USDC).
- **Verdetto: la liquidita' NON e' il collo di bottiglia**; le soglie sono
  prudenti ma non restrittive (si potrebbe alzare la soglia senza uccidere
  il flusso). Riga esplicita nel report: **SCOMMESSE PERSE PER LIQUIDITA'
  = 0/42 (0.0%)**.
- **TARATURA FINALE (decisione 11/09 sera)**: `SX_MIN_EXEC_DEPTH_USDC`
  portata da **10 → 25 USDC** su indicazione del proprietario, proprio
  perche' la misura mostrava che alzare la soglia NON taglia opportunita'
  (nella sensibilita' del misuratore 25 USDC → 42/42 eseguibili, 100%).
  Vale insieme in `sx_signals` (scan) e `auto_bet` (ordine): piu'
  protezione da slippage a impatto nullo sul flusso. `SX_DEPTH_MULTIPLIER`
  resta 2.0; totale match 25 ed esito singolo 5 invariati.

**⚠️ CAVEAT IMPORTANTE — GATE MODELLO non misurabile in locale.** Il DB
locale ha **0 righe in `team_ratings`** (i rating vivono sul volume Railway):
`expected_goals` usa quindi il profilo neutro di lega e il modello non
rappresenta la produzione. Nel campione il gate modello da' 0 segnali value,
ma QUEL numero va misurato sul container. Lo script ora rileva la cosa da
solo: campo `model_gate.measurable` + avviso esplicito nel report quando la
copertura ratings e' 0%.

### Copertura rating sul container + gate modello (11/09/2026, SOLA LETTURA)

Verifica eseguita con `railway ssh --service api -- python3 -c ...`:
sqlite aperto in **`mode=ro`** (URI read-only), nessun ordine, nessuna
scrittura sul ledger, zero crediti the-odds-api (discovery SX pubblica).

**1) ENV su Railway**: `SX_MIN_DEPTH_USDC`, `SX_MIN_LEG_DEPTH_USDC`,
`SX_MIN_EXEC_DEPTH_USDC`, `SX_DEPTH_MULTIPLIER` **non sono impostate**
(`os.getenv` → None) → valgono i default di CODICE. Quindi la soglia 25 e'
attiva appena il codice viene deployato (nessuna env da aggiornare);
`preserve()` in `.railway/railway.ts` serve solo a non farle distruggere se
un giorno verranno impostate.

**2) COPERTURA RATING (il collo di bottiglia vero del gate modello).**
`team_ratings` = **200 squadre / 14 leghe** (ultimo aggiornamento
11/09 01:49), `match_results` = 2725. Profonde solo 9 leghe: MLS 39, Serie A
24, La Liga 23, Eredivisie 19, Premier League 18, EFL Championship 18,
Brasileirao 18, Ligue 1 17, Bundesliga 16; le altre 5 leghe hanno 1-3
squadre (Primeira Liga 1, Champions League 0, Liga MX 0, Russian Premier
League 0, Brazil Serie B 0). Il ledger SX ha 69 partite: **5 con entrambe le
squadre rated (4 con n>=5)**, 19 con una sola, **45 con nessuna**.
Ripartizione SX (n / con entrambe rated): Primeira Liga 10/0, Premier League
7/1, Champions League 6/0, Brazil Serie B 5/0, League One 5/2, Russian
Premier League 5/0, Sweden Superettan 4/0, Liga MX 4/0, Austrian Bundesliga
4/0, Allsvenskan 3/0, Serie B 3/0.

**3) GATE MODELLO LIVE (stesso giro, mercati freschi)**: 39 mercati SX,
36 coerenti (inv_sum 0.98-1.08), **3 candidati favoriti** (fascia 1.30-1.80).
Solo **3/36 coerenti (8.3%)** hanno entrambe le squadre con rating;
**nessuno dei 3 favoriti** ha rating. Edge modello sul favorito di mercato:
mediana **-24.1pp** (p10 -26.5, p90 -13.5), **0/3 con edge >= 0** → **0
segnali value** a qualunque soglia (2pp/3pp/5pp). Interpretazione: su squadre
senza rating `expected_goals` usa il profilo NEUTRO di lega, che su un
favorito netto (quota 1.30-1.80) da' una probabilita' molto inferiore a
quella del mercato: l'edge negativo e' **cecita' del modello**, non un
segnale di valore. Il gate modello NON e' misurabile in modo utile finche'
la copertura resta questa.

**4) SECONDO GAP: NORMALIZZAZIONE DEI NOMI** (anche nelle leghe coperte).
Confronto SX ↔ `team_ratings` sulle partite SX delle leghe coperte — i nomi
spesso NON combaciano (ratio difflib): `Wrexham` vs `Wrexham AFC` (0.78),
`AFC Bournemouth` vs `Bournemouth` (0.85), `Ipswich Town` vs `Ipswich`
(0.74), `Willem II Tilburg` vs `Willem II` (0.69), `Tottenham Hotspur` vs
`Tottenham` (0.69), `AS Monaco FC` vs `Monaco` (0.67),
`Olympique Marseille` vs `Marseille` (0.64); `Nottingham Forest` assente.
Nota collaterale: nel ledger `matches` compaiono righe con league
`Premier League` per club belgi (KV Mechelen, RSC Anderlecht) — residuo di
un mapping pre-fix (il ledger viene riscritto solo quando lo scan rivede il
match: il `repair_sx_leagues()` lo sana).

**5) Conseguenza operativa**: prima di fidarsi del gate modello servono
(a) ratings estesi alle leghe che SX scansiona davvero (CL, Primeira, Liga
MX, Austria, Svezia, Russia, Brazil B, ...) e (b) una mappa alias nomi
squadra SX↔API-Football (stesso pattern deterministico di
`SX_LEAGUE_ALIASES`, mai fuzzy). **→ FATTO l'11/09 sera** (vedi "Fix cecita'
modello: 3 passi"): (b) e' `team_names.py` (coverage 5/69 -> 15/69) e (a)
e' la coppia rosters (`ALL_LEAGUES` 35 -> 47) + `LEAGUE_IDS` (8 -> 41);
resta da eseguire la sync quando l'account API-Football sara' riattivato.
Solo dopo ha senso rilanciare `liquidity_impact.py --with-model` sul
container per numeri rappresentativi.

### Fix cecita' modello: 3 passi (11/09/2026)

Direttiva del proprietario: fermare tutto e chiudere i tre gap emersi dal
report, verificando IN LOCALE prima di pensare al deploy.

**PASSO 1 — repair del ledger (`sx_signals.repair_sx_leagues`).**
Il repair leggeva l'etichetta SX solo per le partite ANCORA sui mercati: i
residui storici (es. 5 partite MLS salvate come `League One` dal fuzzy
vecchio) restavano sbagliati per sempre. Ora c'e' un fallback che deduce la
lega dai ROSTER (`ALL_LEAGUES`) via `_infer_league_from_teams(home, away,
current)`, e la risoluzione nomi (`team_names`) fa agganciare anche i nomi SX
reali (`Atlanta United` -> roster `Atlanta`, `Toronto FC` -> `Toronto`).
Regola PRUDENTE: si sovrascrive SOLO se la lega attuale NON ha nessuna delle
due squadre nel roster (`_roster_support(current) == 0`) e se ESATTAMENTE
una lega contiene entrambe — altrimenti non si tocca nulla. Prova del
perche': con una regola piu' aggressiva il repair aveva trasformato
`Champions League` in `Europa League` per Fenerbahçe–AS Roma (roster CL
incompleto ma etichetta SX corretta). Verificato in locale: `League One` ->
`MLS` su 5/5 residui, `Champions League` intatto, secondo giro `updated 0`
(idempotente). CLI: `venv/bin/python sx_signals.py repair`.

**PASSO 2 — risoluzione nomi squadra (`team_names.py`, nuovo).**
Quattro stadi deterministici, mai un indovinello: (1) match esatto,
(2) `TEAM_ALIASES` espliciti (Nottingham Forest -> Nottm Forest, Spurs ->
Tottenham, PSG -> Paris Saint-Germain, ...), (3) chiave normalizzata
(minuscole, senza accenti/punteggiatura, senza token societari FC/AFC/AC/AS/
KV/RSC/..., senza numeri di fondazione, token ordinati), (4) contenimento di
token con GUARDIA DI AMBIGUITA' (due candidati troppo vicini -> None).
Innescato in `rating_engine.get_rating` (`resolve_team_name`): da li' lo
usano `poisson_engine.expected_goals`, `sx_signals.scan`, `auto_bet` e
`liquidity_impact`. Fail-safe totale: qualunque errore o dubbio ricade sul
nome originale (profilo neutro), mai su un rating sbagliato.
Misura sui DATI REALI del container (69 partite SX, 200 nomi rating):
partite con ENTRAMBE le squadre rated **5/69 (7.2%) -> 15/69 (21.7%)**, 3x.
Le non risolte sono squadre ASSENTI da `team_ratings` (Brazil Serie B,
Superettan, CL: Fenerbahçe, Shakhtar, Slavia Praha...): e' il gap di
copertura del passo 3, non un problema di nomi.

**PASSO 3 — copertura leghe (`football_hist.py` + `leagues_data.py`).**
`LEAGUE_IDS` passa da 8 a **41 leghe**: coppe europee (CL 2, EL 3,
Conference 848, Libertadores 13, Sudamericana 11), cadetterie e campionati
che SX scansiona (Championship 40, League One 41, Serie B 136, Primeira Liga
94, Liga MX 262, J1 98, K League 1 292, Saudi 307, Swiss 207, Superliga DK
119, Allsvenskan 113, Eliteserien 103, A-League 188, Veikkausliiga 244,
Argentina 128, Chile 265, Colombia 239, Egitto 233) piu' le 10 chiuse
l'11/09 sera (Austria 218, Russia 235, Turchia 203, Belgio 144, Scozia 179,
Grecia 197, Polonia 106, 3. Liga 80, Superettan 114, Brazil Serie B 72).
**Roster aggiunti in `ALL_LEAGUES` (35 -> 47 leghe):** Austria, Russia,
Turchia, Belgio, Scozia, Grecia, Polonia, 3. Liga, Superettan, Brazil Serie
B, League One, Copa Sudamericana (~150 squadre con profili prior, costruiti
con l'helper `_profiles`). Senza roster `_match_db_name` non allinea nessuna
squadra: la sync brucerebbe richieste salvando 0 righe. Tripwire:
`test_football_hist.test_ogni_lega_sincronizzabile_ha_un_roster` (ogni id in
`LEAGUE_IDS` DEVE avere un roster non vuoto) + `test_leghe_dei_buchi_ora_in_
all_leagues`. In piu' `LEAGUE_EFFICIENCY` (market_calib) ha ora gli score
delle nuove leghe per il blend dinamico. Aggiunte tre difese:
- **validazione id→lega** (`LEAGUE_API` + `_league_response_ok`): la sync
  confronta nome/paese restituiti dall'API con l'atteso e SALTA la lega se
  non combaciano — un id sbagliato non puo' importare un'altra competizione
  sotto il nome sbagliato;
- **marker di sincronizzazione** (tabella `sync_state`, per lega+stagione,
  scritti SOLO dopo il salvataggio): niente piu' 30 leghe riscaricate ogni
  giorno (free plan 100 req/giorno). `FORCE_HISTORY_SYNC=1` li ignora;
- **memo stagioni** in-process: la prima lega scopre dove comincia la
  copertura del piano, le successive partono da li' (2 richieste/lega in
  meno). Nuova CLI `--verify-ids` (una chiamata per lega, nessuna scrittura)
e `--reset-markers`.

**🔑 ROTAZIONE CHIAVE API-FOOTBALL (11/09 sera) — account ANCORA sospeso.**
Il proprietario ha resettato la chiave e l'ha incollata in chat: la chiave e'
stata considerata **compromessa** (regola 7) e va ruotata di nuovo appena la
sync funziona (Opzione A: lui la imposta da `railway variables --service api
--set`, l'agente verifica soltanto). La variabile e' stata aggiornata su
Railway con `railway variables --service api --set API_FOOTBALL_KEY=...`
(`API_FOOTBALL_KEY: preserve()` era gia' dichiarata in `.railway/railway.ts`,
quindi `railway config apply` non la distrugge) e il redeploy automatico ha
girato (deployment `afd45ab2-3338-4fd1-b703-7deea1122751`).
Verifica SENZA esporre la chiave: confronto dell'impronta sha256
(`len 32`, `sha256[0:12] = 5a6df8a8325e` identica a quella del valore atteso)
+ chiamata live a `/status`. Esito: **la chiave e' arrivata correttamente ma
l'account e' ancora sospeso** — `{'access': 'Your account is suspended,
check on https://dashboard.api-football.com.'}`, stesso errore dal container
e da una rete indipendente.  \nConseguenza: `--verify-ids` e la sync NON sono
stati lanciati (avrebbero dato 41/41 BAD bruciando quota per nulla). Il
reset della chiave NON rimuove la sospensione dell'account: va sbloccata dal
dashboard (o aprendo un ticket / con un account nuovo); tutto il resto e'
pronto e verificato, basta lanciare i due comandi quando l'API risponde.

**⚠️ BLOCCO ESTERNO SUL PASSO 3 — account API-Football SOSPESO.**
Verificato sia in locale sia SUL CONTAINER con una chiamata reale a
`/leagues`: `{'access': 'Your account is suspended, check on
https://dashboard.api-football.com.'}` (chiave presente, 32 char, ma
rifiutata; `--verify-ids` -> 0/30). Quindi il job giornaliero
`history_sync_job` (08:30 ITA) e' fermo da tempo: ecco perche' le leghe
extra sono vuote. La sync NON ha mai importato dati sbagliati (salta la
lega e logga), ma finche' l'account non viene riattivato il passo 3 resta
*implementato e non eseguito*: dopo la riattivazione basta
`venv/bin/python football_hist.py --verify-ids` e poi `/sync` (o
`football_hist.py --seasons 2`).

**STATO CONTAINER — DEPLOY + REPAIR ESEGUITI (11/09 sera).**
Prima del push il container girava codice SENZA `SX_LEAGUE_ALIASES`,
`repair_sx_leagues`, `team_names` (verificato via `railway ssh`: attributi
assenti). Push su main `82aaa35..da9e88e` -> deploy automatico Railway
(deployment `8fc2711b-c126-4ddb-8239-d00a20779a14`), verificato con
`railway ssh ... python3 -c "import team_names"`.
Repair eseguito SUL container (`python3 sx_signals.py repair`):
**`{'checked': 118, 'updated': 17, 'inferred': 5}`** — 5 residui MLS
(`League One` -> `MLS`, inferiti dai roster) + 1 residuo belga
(`Premier League` -> `Belgian First Div`) + 11 righe corrette dall'etichetta
SX viva (`Liga Profesional` -> `Argentina Primera`, `Primera Division` ->
`Chile Primera`, `Primera Nacional`/`Primera A` = etichette SX grezze non
coperte, prima mappate per errore su `Primeira Liga`).
Verifica post-deploy sul container: kill-switch `effective: off`,
settlement in pausa, `ODDS_MIN/MAX 1.3/1.8` + favouriti only, cap
`1%/2%` con cap severo attivo, liquidita' `25/5/25/x2.0`, `team_names`
popolato. Ledger SX: 25 leghe distinte, **23/25 con sport key
the-odds-api** (restano fuori solo `Primera A` e `Primera Nacional`, non
coperte per scelta). `team_ratings` invariato a 200 (il repair non tocca i
rating). Health API 200.
⚠️ Crediti the-odds-api al momento del deploy: **58 residui** — con
settlement in pausa e kill-switch off il consumo e' solo la rotazione value
(~3,5/giorno).

**Verifica finale in locale (prima del deploy)**: 338 test verdi sul set
ampio + 130 sul set mirato (league_mapping, team_names, football_hist,
odds_api, rating_engine, poisson_engine, sx_signals, market_calib); tutti
gli id hanno roster; nessun roster vuoto in `ALL_LEAGUES`.
### Rotazione chiave API-Football + prima sync storica (12/09/2026)

**1) La chiave "nuova" era nel PROGETTO SBAGLIATO.** Esiste un **secondo
progetto Railway** (`valiant-liberation`) con un servizio chiamato
`quotaverace`: le modifiche fatte via RAW Editor e CLI finivano LÌ, non su
`quotaverace/api` (impronta `5a6df8a8325e` rimasta identica per 24h). Il
servizio orfano gira la stessa immagine ma crash-loopava su `ValueError:
Token non configurato.` perche' `config.py:53` legge `QUOTAVERACE_BOT_TOKEN`
mentre la' le variabili si chiamavano `TOKEN`/`BOT_TOKEN`/`TELEGRAM_TOKEN`
(mai lette → il crash-loop NON era il bot di produzione: lo scheduler di
`api` non si e' mai riavviato, ancoraggio `:25.416895` continuo).
⚠️ **Lezione operativa**: `railway list` prima di inseguire i log — con due
progetti attivi i log possono essere di un container estraneo.
Chiave valida trovata nel progetto orfano (`API_FOOTBALL_KEY` sha12
`fc8972c3a59e`; `/status` → `errors: []`, account "giuseppe bona", **piano
Free attivo fino al 12/09/2027**) e copiata su `quotaverace/api` con pipe
diretto fra i due progetti (valore MAI stampato in chat). Redeploy
`4d2dc56c` SUCCESS, container sano, nessun `ValueError`. **Progetto orfano
ELIMINATO** (`railway delete -p <id> --yes`, deletedAt programmato).
La chiave vecchia (`5a6df8a8325e`) e' morta: rigenerare la chiave invalida la
precedente (`Error/Missing application key`).

**2) Rate limit del piano Free: 10 richieste/MINUTO** (oltre alle
100/giorno). Sono limiti DIVERSI e il primo uccide i burst: `--verify-ids`
(41 chiamate di fila) ha dato **9/41 OK e 32 BAD** con `rateLimit: Too many
requests`. Non lanciare mai loop API-Football senza pacing.

**3) DUE BUG REALI trovati dalla sync** (silenziosi: `_league_response_ok`
scarta la lega e la sync importa 0 righe senza errore):
- `LEAGUE_API["La Liga"] = ("laliga", "spain")` ma l'API dice `La Liga` →
  **La Liga veniva saltata in OGNI sync**;
- `Turkey Super Lig` atteso `"super lig"` ma l'API dice `Süper Lig` (la
  dieresi rompe il contenimento); `Argentina Primera` atteso `"primera"` ma
  l'API dice `Liga Profesional Argentina`.
Corretti (le due leghe ora validano il solo PAESE). Tripwire:
`test_football_hist.TestNomiApiVerificati` (nomi reali API del 12/09).

**4) PACING del rate limit** (`football_hist._throttle`): ogni chiamata
`_api_get` (incluso il fallback di settlement in `sx_signals`) e' distanziata
di `API_FOOTBALL_MIN_INTERVAL` secondi (default **6.5** ≈ 9/min; `0` =
disattivato; environment dichiarata in `preserve()` in `.railway/railway.ts`).
Prima un burst bruciava richieste nei retry e faceva saltare le leghe. Test:
`test_football_hist.TestRateLimitThrottle` (default/env/attese).

**5) SYNC STORICA ESEGUITA (12/09, a lotti con pacing)**: 35 leghe,
**12.467 partite salvate**, zero leghe saltate (3 lotti: 5.791 + 3.933 +
2.743). `match_results` 2.725 → **15.192**; `team_ratings` 200 → **622
squadre su 40 leghe**; `sync_state` = **33 leghe marcate** (il job 08:30 non
le riscarica). Restano le 8 leghe core gia' profonde (Serie A, PL, La Liga,
Bundesliga, Ligue 1, Eredivisie, MLS, Brasileirao): le prende il job
automatico. Quota residua il 12/09: **31 richieste**.
⚠️ NB: il conteggio per lega in `team_ratings` ora e' "sporcato" dalle coppe
(una squadra che gioca in piu' competizioni prende la prima etichetta del
set: Brasileirao 18 → 2, Europa League 52): il gate usa il NOME SQUADRA, non
la lega, quindi nessuna regressione — le squadre rated sono triplicate.

### Settlement nativo SX + favoriti netti positivi nel backtest + scadenza righe stale (12/09 pomeriggio/sera)

**1) SETTLEMENT NATIVO SX (commit `0305ffc`, deployato e VERIFICATO in produzione).**
the-odds-api NON copre le leghe di alcune bet sx-* (Colombia Primera A,
Primera Nacional Argentina, K League 2: assenti dal catalogo /v4/sports —
verificato). Ma per le bet `sx-*` l'esito lo decide **SX stesso**: la fonte
corretta in assoluto per il nostro denaro e' il market risolto
sull'exchange, GRATIS (zero crediti). Implementato in `sx_signals.py`:
- `_results_from_sx(provider)`: (a) `markets/find` a BATCH di 30 sui
  `market_id` salvati sulle bet (bets.market_id = marketHash, li porta
  gia' dall'ordine) -> `outcome` 1|2 + `teamOneScore/teamTwoScore` per i
  mercati risolti; (b) fallback punteggi live su `markets/active` SOLO a
  >= 120' dal kickoff (prima: il punteggio live di un match in corso e'
  un verdetto sbagliato — test dedicato); (c) bet orfana senza riga in
  `matches` (es. bet #21 America MG–Nautico): ricostruita da zero
  (_same_event -> save_match -> save_result), la find ha anche rivelato
  la lega vera (Brasileiro Serie B). Semantica outcome: relativo alla
  gamba binaria del mercato (su "T1 vs Not T1": 1 = vince T1, 2 = non
  vince T1). Sempre `same_event`/`_norm_team` per l'aggancio, mai
  inversione casa/trasferta; guardia di unicita' come nel path esterno.
- **Gate attivazione**: il percorso SX-native segue le stesse regole delle
  altre fonti — attivo SOLO se `ODDS_API_KEY` o `API_FOOTBALL_KEY` sono
  configurate (i test offline restano network-free senza toccarli);
  disattivabile con `SX_NATIVE_SETTLEMENT=0`.
- Ordine fonti in `settle_sx_bets`: SX-native (gratis) -> the-odds-api
  (solo match ANCORA senza risultato sx-*, `missing`) -> API-Football
  (fallback). Poi `settle_bets` + `settle_predictions` (chiusura per
  match_id). Clamps per i test: `_sx_open_matches()` ora accetta
  `conn=None`, `_results_from_sx` usa `_create_results_table`.
- **Verificato IN PRODUZIONE** (job background post-deploy, 18:35 UTC):
  #21 America MG ✅ +2.25, #33 Jaguares ❌ −1.00, #34 Santa Fe ✅ +1.46
  → **netto +2.71 USDC, zero crediti**. #35 Atalanta e #36 CSKA si
  saldano da soli coi giri normali (erano in gioco al momento del check).
- Test: `test_sx_native_settlement.py` (18 test: find batch, live window,
  bet orfana, leghe non coperte, pausa, gate offline).

**2) ANALISI LEGHE — LA FASCIA FAVORITI E' POSITIVA NEL BACKTEST STORICO.**
Run walk-forward 2022–2026 (16.273 partite, `historical_backtest.py
--no-ou --max-odds 1.8 --save`, modello locale senza rating reali =
misura della sola macchina blend vs closing line reali): **215 bet,
ROI flat +5.9%, hit 60.5%, MaxDD 7.1%** (equity 100 → 112.7 senza mai
scavare). Tagli di robustezza (stake Kelly variabili -> P/L / STAKED):
- **Bucket quota (LA combinazione vincente): 1.60–1.80 = +21.9% ROI su
  55 bet (staked +18.5%), hit 65.5%** — e' il cuore del sistema.
  1.30–1.45 +7.4% (n=44), 1.45–1.60 **−9.9% (n=49, il pantano: solo
  12 con edge>=5pp)**, 1.60–1.80 +21.9% (n=55, 27 con edge>=5pp).
- Casa +9.0% (n=171) vs Trasferta −4.6% (n=44, p-value 0.08: non
  significativo ma orientato come la letteratura).
- La combo 1.60–1.80 ∩ edge>=5pp e' POSITIVA in TUTTE le 4 stagioni
  (n=25, +26.5% staked): consistente, non un anno fortunato.
- Confronto con lo stato AGENTS dell'11/09 ("produzione: 0 bet, il
  modello NON batte la closing line"): la differenza e' la FASCIA —
  quel run misurava l'universo lungo (fino a 5.0). Sui SOLO favoriti
  netti il gate e' positivo. ⚠️ Campione piccolo (215), staking Kelly
  1/4 cap 1%, niente look-ahead; e il gate live richiede edge >= 3pp
  (piu' selettivo del >= 5pp della combo migliore).
- Campiono reale sul container (liquidita', --with-model): 26 favoriti
  in fascia, 24/26 eseguibili (2 persi per profondita', −7.7%); edge
  positivi sul favorito 22/113 (p90 +12.5pp); 0 segnali value completi
  nel campione di 120 (la catena blend+fascia+EV+edge filtra tutto,
  come atteso con 5pp reali rari). CSV delle bet: data/
  historical_backtest_bets.csv (rigenerato col gate 1.30–1.80).
- **Non e' stato cambiato nessun gate di produzione** (solo evidenza
  di ricerca): eventuale restrizione futura della fascia live a
  1.60–1.80 = decisione del proprietario.

**3) SCADENZA RIGHE SX-* STALE (commit `d94bb6a`) — il buco crediti
chiuso.** Misurato sul container: 44 match sx-* aperti, 38 in finestra
(saldabili), **6 orfani fuori finestra da giorni** (batch 09/09 senza
riga in `matches`): a ogni giro entravano in `missing` e provocavano
`fetch_scores` (2 crediti) che NON li avrebbe mai trovati (la finestra
the-odds-api e' di 3 giorni). Nuovo `tracker.expire_stale_sx_rows()`:
- Riga SCADUTA = `match_id LIKE 'sx-%'`, `esito_finale IS NULL`,
  kickoff (commence_time in `matches`) piu' vecchio di `SX_STALE_DAYS`
  giorni (default **5**, letta a RUNTIME: env override senza redeploy)
  e NESSUN risultato in match_results → chiusa come **push (P/L 0)**:
  nessun verdetto inventato, solo lo stop ai costi.
- **Ramo orfani**: righe sx-* senza `matches` usano `created_at` come
  riferimento temporale (senza questo ramo le 6 righe del batch 09/09
  resterebbero aperte per sempre).
- Chiamata in CODA a `settle_sx_bets` (DOPO le fonti e i settle): se una
  fonte ha appena salvato il risultato, la riga e' gia' chiusa col
  verdetto vero — mai scadere una riga che una fonte stava per chiudere.
  Ritorno: campo `expired` = {bets, predictions} nel dict del settlement.
- Pausa settlement rispettata (0 righe scadute in pausa). Test: 18 in
  test_sx_native_settlement (incl. orfani scaduta/non-scade, pausa,
  SX_STALE_DAYS runtime).

**4) STATO OPERATIVO 12/09 sera.** Wallet 35.9978 USDC (exposure 2.0 =
le 2 bet del giorno), P/L live da ripartenza −3.13 USDC su 11 chiuse
(3 vinte, 8 perse — le perse quasi tutte pre-cambio-strategia a quota
3.0–4.35). Crediti the-odds-api ~450 con reset 01/10. Kill-switch live,
settlement attivo, STAKE_CAP_HARD=0 (floor 1 USDC su wallet 38).

### Riavvio bot + fix contatore crediti (12/09/2026)

**1) RIPRESA DELL'OPERATIVITA' (12/09, verificata sul container).**
- `data/execution/settlement_paused.json` -> `{"paused": false}` e
  `data/execution/auto_bet_mode.json` -> `{"mode": "live"}`: kill-switch
  `effective: live`, `provider_ready: true`, `_execution_mode()` = live,
  settlement RIATTIVATO. Nessun redeploy necessario (i job leggono i file a
  ogni giro).
- Primo giro di settlement reale: **72 partite aggiornate**, 17 previsioni
  saldate, 1 bet live (Venezia) saldata. Costo: ~52 crediti the-odds-api
  (26 leghe x ~2 crediti) -> **remaining 6** (reset 01/10). I 10 sx-* bets
  ancora aperti NON si saldano con `bot._update_results`: le bet con
  `match_id` sx-* passano da `sx_signals.settle_sx_bets()` (risultati
  salvati con l'id SX), che ha bisogno di credito per lega.
- Il bot gira e valuta i candidati: nei log `auto_bet: bankroll LIVE =
  saldo wallet 38.00 USDC` e **2 candidati favoriti ammessi dal gate**
  (uno strong su `sx-L19947936`) ma **0 ordini** per il CAP SEVERO
  (`stake 0.38/0.76 USDC < minimo ordine 1.00`). Con wallet 38 USDC il cap
  1%/2% non e' sostenibile: servono >= 100 USDC (1%) o >= 50 (2%), oppure
  `STAKE_CAP_HARD=0` per accettare il floor da 1 USDC (2.6% del bankroll).
  La decisione e' del proprietario: la configurazione attuale e' live ma
  senza ordini.

**2) BUG FIXATO — il contatore crediti era CIECO al consumo del settlement
(`odds_api.fetch_scores`).** `fetch_scores` salvava la cache punteggi come
`{"ts", "payload"}` SENZA `remaining`, mentre `_get_odds` lo salva: quindi
`get_remaining()`/`get_quota()` (minimo fra le cache `toa_*.json`) vedevano
solo il consumo della rotazione quote. Misura reale del 12/09: il contatore
diceva **58** mentre l'ultima risposta dell'API riportava **6** -> la guardia
proattiva `should_query_sport` NON ha ridotto le leghe e il credit watchdog
(`credit_watchdog_job`, soglie 50/20/10/5) non ha mai allertato: i crediti
sono stati bruciati dal settlement fino quasi a zero senza nessun campanello.
Fix: la cache punteggi registra anche `remaining` (con commento). Tripwire:
`test_odds_api.test_scores_cache_persiste_i_crediti` (fake response con
header `x-requests-remaining: 6` -> cache con `remaining`, `get_remaining()`
e `get_quota()` coerenti). Nota: le cache gia' scritte senza il campo restano
cieche finche' non vengono riscritte (un fetch le aggiorna).
`ODDS_DAILY_BUDGET` aggiunta in `preserve()` di `.railway/railway.ts`.

**3) Numeri utili del giro.** Wallet SX **37.9978 USDC** (exposure 0),
crediti the-odds-api **6** (reset 01/10), `team_ratings` **622** squadre,
`match_results` 15.192, 186 previsioni aperte su 26 leghe (23 mappabili; le
non mappabili sono scelte: `Primera A`, `Primera Nacional`, `K2-League`),
10 bet live aperte tutte pre-cambio-strategia (quote 2.09-4.35).

### Decisioni del proprietario 12/09/2026 (ripartenza degli ordini reali)

- **`STAKE_CAP_HARD=0`** (env su Railway, dichiarata in `preserve()`):
  scelta esplicita del proprietario per far ripartire gli ordini senza
  top-up. I cap PERCENTUALI restano 1% (value/moderate) e 2% (strong_value),
  ma ora il floor dell'exchange (1 USDC) prevale: su un wallet di 38 USDC
  ogni bet e' ~2.6% del bankroll. I risk cap di portafoglio sono intatti
  (correlazione 30% per blocco, esposizione totale 40%/giorno).
  ⚠️ Da rivedere se il wallet cresce (con >= 100 USDC il cap severo torna
  sostenibile e si puo' rimettere `STAKE_CAP_HARD=1`).
- **Prima bet reale della fase ripartita** (12/09 15:22 UTC): Atalanta vs
  Cagliari, esito `1` @ **1.75**, **1.00 USDC**, `FULLY_FILLED`
  (tx `0x0bea0e28…`). Wallet 37.9978 -> **36.9978** (exposure 1.0).
  Secondo candidato nella stessa tornata (CSKA Moscow vs Rubin Kazan,
  `sx-L19947936`, `1` @ 1.69): ordine **CANCELLED non riempito** -> nessuna
  riga a ledger e nessun ordine in eccesso (floor EV rispettato).
- **Crediti the-odds-api**: il proprietario crea una **chiave/account nuovi**
  (500 crediti freschi) e la imposta da se' (mai in chat, regola 7):
  `railway variables --service api --environment production --set-from-stdin ODDS_API_KEY`
  dalla root del repo (incolla il valore e Ctrl-D). Dopo il set: verificare
  la nuova impronta, poi lanciare il settlement delle righe rimaste aperte
  (10 bet live pre-cambio-strategia + ~186 previsioni) e riattivare la
  rotazione quote.

### Chiave the-odds-api nuova + settlement SX riparato (12/09/2026)

**1) CHIAVE NUOVA VALIDA.** Impronta `5c483976d988` (len 32), letta
  identica dal container dopo il redeploy. Verifica a costo ZERO sull'endpoint
  `/v4/sports` (non consuma crediti): **HTTP 200, `x-requests-remaining: 500`,
  used 0, 87 sport** → account nuovo, quota azzerata. Il fix del contatore
  crediti (deploy `f278844`) ora vede davvero il residuo.

**2) LE BET NON SI SALDAVANO: 3 cause, tutte misurate sul container.**
Con 500 crediti disponibili si poteva finalmente refertare, ma il giro
`_update_results` + `settle_sx_bets` scaricava 72 partite e chiudeva **0
righe**: i risultati c'erano (o erano a un passo) ma l'abbinamento falliva.
  1. **NOMI DIVERSI tra SX e the-odds-api** (la causa principale):
     `Cienciano` vs `Club Cienciano`, `CR Flamengo` vs `Flamengo-RJ`,
     `Vila Nova GO` vs `Vila Nova`, `Velez Sarsfield` vs `Velez Sarsfield BA`,
     `Corinthians SP` vs `Corinthians-SP`, `Newell's Old Boys` vs
     `Newells Old Boys`, `Goias` vs `Goiás`. `_norm_team`/`_loose_team` NON
     coprivano apostrofi, codici di stato (RJ/SP/GO/BA) e sigle diverse.
  2. **FINESTRA TROPPO CORTA**: `fetch_scores(sport, days_from=2)` escludeva
     le partite a >48h (es. Libertadores di due sere prima) — il massimo
     consentito dall'API e' **3** e il costo per chiamata NON cambia.
  3. **SOLO LE BET**: `_sx_open_matches()` guardava la tabella `bets`, quindi
     le previsioni SX dei match **senza puntata** restavano aperte per
     sempre (156 `rejected` + 6 value aperte): inquinavano la telemetria di
     calibrazione dei mercati.

**3) FIX (deploy verificato).**
  - `team_names.same_team(a, b)`: confronto **SIMMETRICO** tra due nomi di
     provider diversi (senza roster di verita'), a stadi deterministici —
     grezzo case-insensitive, `normalize`, `_core` (nuovo: toglie
     `REGION_CODES` RJ/SP/GO/BA/... e `JOIN_PARTICLES` de/del/do/da/los/...),
     poi contenimento di token. `normalize` ora **cancella l'apostrofo
     PRIMA della punteggiatura** ("Newell's" non diventa piu' `newell s`,
     che non agganciava 'Newells'). Mai fuzzy: `same_team` da' False per
     'Manchester United' vs 'Manchester City'.
  - `sx_signals._same_event()`: stadi `_norm_team` → `_loose_team` →
     `same_team`, **mai inversione casa/trasferta**.
  - **GUARDIA DI UNICITA'**: il confronto tollerante puo' agganciare piu'
     partite ("Manchester" sta in United e City). Se i candidati sono >1 la
     riga **RESTA APERTA** con warning: meglio un ritardo nel ledger che un
     verdetto col risultato di un'altra partita. (Il match tollerante senza
     guardia sarebbe stato un rischio di falsi positivi.)
  - `odds_api.SCORES_DAYS_FROM = 3` (default di `fetch_scores`) usato sia da
     `bot._update_results` sia dal settlement SX.
  - `_sx_open_matches()` = **UNION** bet + previsioni aperte (i match con
     sole previsioni ora ricevono il risultato) e `settle_sx_bets()` chiama
     anche `settle_predictions()`: in un giro chiude bet E previsioni.

**4) Test**: `test_team_names.TestSameTeam` (12 coppie reali parametrizzate +
  "mai fuzzy" + contenimento ambiguo documentato),
  `test_league_mapping.TestSettlementNomiTolleranti` (prefisso club, codici
  di stato + apostrofo, guardia di unicita', previsioni senza bet, finestra
  3gg), `test_settlement_source` aggiornato (import di `bot.py` su piu'
  righe). Focus verde: team_names + league_mapping + sx_signals +
  settlement_* + odds_api + scores_cache + bot + football_hist + rating/
  poisson engine.

**5) ⚠️ LIMITE NOTO — leghe non coperte da SPORTS_MAP.** Restano
  strutturalmente non saldabili le righe SX su **`Primera A` (Colombia)**,
  **`Primera Nacional` (Argentina)** e **`K2-League`**: il settlement le logga
  ("leghe non mappate... bet lasciate aperte") e NON brucia crediti. Per
  saldarle serve aggiungere la competizione a `SPORTS_MAP` (decisione di
  copertura/crediti, non un fix). Stesso limite per le righe **senza riga in
  `matches`** (21 previsioni + 1 bet del batch 09/09 23:03: senza nomi
  squadra non c'e' nulla da abbinare).
  ⚠️ **Verificare sempre che le soglie di liquidita' non blocchino il
  flusso**: la copertura rating e' cresciuta (622 squadre), quindi il gate
  modello e' ora misurabile.

**ESITO sul container (12/09, deploy `ce7823a0`).** Cache punteggi
  invalidate a mano (erano scritte col vecchio `days_from=2`), poi
  `_update_results()` + `settle_sx_bets()`:

| | prima | dopo |
|---|---|---|
| Bet live aperte | 10 | **5** |
| Bet saldate oggi | 3 | **8** |
| Previsioni aperte | 182 | **93** |
| Previsioni saldate oggi | 0 | **114** |
| Crediti the-odds-api | 500 | **452** |
| `match_results` aggiornati | — | 101 partite |

Le 5 bet ancora aperte: 2 sono di **Primera A (Colombia)**, 1 senza riga
  in `matches` (batch 09/09), 1 e' **CSKA–Rubin** (kickoff 17:45, in corso) e
  1 **Atalanta–Cagliari** (18:45) — le ultime due si saldano al giro
  successivo del job, senza intervento (la finestra 3gg le copre).

**Coda residua (13 match SX con kickoff passato ancora aperti)** — cause
distinte, tutte "non saldabili con le regole attuali", nessuna silenziosa:
  1. **Competizioni non coperte da `SPORTS_MAP`** (5 match + 2 bet):
     `Primera A` (Colombia), `Primera Nacional` (Argentina 2), `K2-League`
     (Korea 2). Serve una decisione di copertura/crediti: aggiungere la
     competizione a `SPORTS_MAP` + roster in `ALL_LEAGUES` + `LEAGUE_IDS`.
  2. **Etichetta di lega sbagliata nel ledger** (2 match): residui
     pre-fix-mapping (es. Millonarios–Deportivo Cali salvato come
     `Primeira Liga`, Union Berlin–Schalke come `Austrian Bundesliga`): il
     settlement interroga la competizione SBAGLIATA. `repair_sx_leagues()`
     e' conservativo e ha restituito `updated 0` (`{'checked': 127,
     'updated': 0, 'inferred': 0}`) — non indovina, quindi restano li'.
  3. **Payload API senza la partita** (1 match): la Champions del 10/09 non
     compare in `scores` (`soccer_uefa_champs_league` ha risposto SOLO con
     le partite future di ottobre) — limite del piano/API, non del codice.
  4. **Varianti di nome non coperte in modo deterministico** (3 match):
     `BB Erzurumspor` vs `Erzurum BB`, `FC Kopenhagen` vs `FC Copenhagen`,
     `FC Red Bull Salzburg` vs `RB Salzburg`. Coprirle richiede uno stadio
     di similarita' (rischioso: falso positivo = verdetto di un'altra
     partita) → possibile in futuro SOLO con threshold alta + guardia di
     unicita' gia' presente.
  5. **Senza riga in `matches`** (7 match / 21 previsioni + bet #21): niente
     nomi squadra, quindi niente da abbinare (recuperabili solo se il
     mercato SX e' ancora attivo e leggibile).
  6. **Future o in corso**: ~22 match (12-13/09) — correttamente aperti.

**BONUS fix contatore crediti (12/09, `get_remaining`).** Con la chiave nuova
  l'API riportava **452** crediti ma `/api/health` e la guardia proattiva
  continuavano a leggere **58**: `get_remaining()` prendeva il **MINIMO** tra
  tutte le cache `toa_*.json`, e le cache quote scritte con la chiave VECCHIA
  tengono quel valore per 3-30 giorni (si rinnovano per lega). Ora vale la
  lettura **piu' recente**: nuovo campo `remaining_ts` scritto da
  `_get_odds`/`fetch_scores` (in `fetch_scores` il `ts` della cache puo'
  essere preservato da un giro precedente, `remaining_ts` e' sempre il
  momento della chiamata), con ripiego su `ts` per le cache di formato
  vecchio. Stessa helper (`_latest_credits`) anche per `get_quota()`: la
  `/api/health` mostrava il valore stantio perche' duplicava il minimo.
  Verificato post-deploy: `get_quota()` = `(452, 89)` e health
  `"remaining": 452`. Tripwire:
  `test_get_remaining_usa_la_lettura_piu_recente`,
  `test_get_remaining_senza_remaining_ts_usa_ts`.

### Audit completo + 502 in produzione + guardrail ripristinati (13/09/2026)

**1) PRODUZIONE GIU' (502): conflitto di merge COMMITTATO.** `origin/main`
  conteneva le righe `<<<<<<< / ======= / >>>>>>>` dentro `auto_bet.py`
  (commit `2c42baa`, dalla serie "FREQUENZA boost" `40deffb`/`742bb9f`/
  `2c42baa`): il container crashava all'import → **502 su tutto** (bot fermo,
  nessun settlement, nessuna puntata) e nessuna notifica Telegram (il bot e'
  morto col container). `origin/main` differiva dal commit locale **SOLO** per
  quelle 4 righe. Fix: merge risolto (commit `81f2bff`), con verifica che
  l'albero finale sia IDENTICO al fix locale (`git diff --stat <fix> HEAD`
  vuoto — il primo merge aveva ripreso il test vecchio, corretto con
  `git checkout <fix> -- test_favourites_only.py` + `--amend`).
  **Regola permanente (nuova regola 8):** prima di ogni push,
  `grep -rn "^<<<<<<<" *.py` deve essere vuoto.

**2) BUG REALE nel commit "FREQUENZA boost": `dynamic_kelly_stake` scalava
  l'EV DUE VOLTE** (`frac = dynamic_kelly(ev, ...)` e poi
  `ev_factor = ev/0.20`), quindi lo stake finiva **sempre** sul floor
  dell'exchange: con wallet 38 USDC 0.07–0.35 → **1.00 USDC = 2.63% del
  bankroll**, cioe' il CAP SEVERO 1%/2% e il fail-closed di `STAKE_CAP_HARD`
  erano **di fatto disattivati** (fail-open silenzioso). Misura: bankroll 38 →
  1.00; 100 → 1.00; 500 → 1.00–14.53 (solo con bankroll grande il floor
  smetteva di mordere). **Ripristinato l'adaptive staking** (`adaptive_stake`,
  cap severo rispettato), conservando timing filter (`get_optimal_timing`),
  odds movement bonus (+20% stake su quota a -5%) ed exposure control
  (`calculate_exposure`). Rimossa `dynamic_kelly_stake`;
  `value_filter.dynamic_kelly` resta esportata (retrocompatibilita' test).

**3) GUARDRAIL 11/09 RIPRISTINATI.** Il commit "FREQUENZA boost" aveva
  rilassato le soglie **e anche le asserzioni del tripwire che le
  proteggevano** (`assert ODDS_MAX <= 2.00`, `MARKET_EDGE_MIN >= 0.02` — col
  docstring che continuava a dichiarare "1.30-1.80 / +3pp": il tripwire non
  proteggeva piu' nulla). Ripristinati i valori della direttiva prudente:
  - `ODDS_MAX` 2.00 → **1.80** (con `ODDS_MIN` 1.30 = fascia favoriti netti)
  - `MARKET_EDGE_MIN` 0.02 → **0.03** (+3pp), `MARKET_EDGE_MODERATE` → **0.03**
  - `MARKET_EDGE_STRONG` 0.04 → **0.05** (+5pp strong_value)
  - `DEFAULT_LEAGUE_STRATEGY.min_edge` 0.02 → **0.03**
  Tripwire riallineato: `test_risk_guards.TestFasciaFavoriti` asserisce
  `ODDS_MAX == 1.80`, `MARKET_EDGE_MIN >= 0.03`, `MARKET_EDGE_STRONG >= 0.05`;
  `test_edge_sotto_3pp_bocciato` (+1pp e +2pp bocciati, +3pp passa);
  `test_value_filter` (`ODDS_MAX == 1.80`, `min_edge fallback 0.03`,
  `test_quota_2_00_bocciata`). La webapp (`webapp/app/value/page.tsx`) diceva
  gia' "1.30-1.80 / +3pp": ora e' di nuovo vero. ⚠️ Conseguenza ATTESA:
  **meno segnali** (gate piu' selettivo) — e' la scelta prudente, non un bug.

**4) ALTRI FIX.** Testi dei filtri Telegram derivati dalle costanti reali
  (`bot.FILTRI_TXT` → "EV 2%-20% | Odds 1.30-1.80 | Edge ≥ +3pp | Kelly
  frazionato | Cap 0.5-2%") invece che hardcoded; `historical_backtest._kelly_stake`
  importava `KELLY_FRACTION` da `value_filter` (inesistente → ImportError),
  ora usa la frazione locale.

**5) VERIFICA FINALE (13/09).** Suite completa **1050 test verdi** su 63 file
  (in lotti: la suite intera supera il timeout di 10' della shell — NON
  utilizzabile in background, i processi figli vengono terminati), 0 marker di
  conflitto, `compileall` OK. Produzione: `/api/health` **200**, scheduler +
  tutti i job registrati, ensemble retrain **n=219** (Brier 0.0448, acc 0.982),
  calibrazione isotonica **65 campioni OOF** (0.2691 → 0.2145), backup
  integrity ok, kill-switch `effective: live` + `provider_ready: true`,
  settlement NON in pausa, wallet SX **35.98 USDC**. Ledger: 38 bet (2 aperte,
  entrambe live), 107 previsioni aperte, `match_results` 15.378,
  `team_ratings` 661, `matches` 316. Contatore crediti **410**
  (`get_remaining()`; `/api/credits` mostra ancora `remaining_min` 58 =
  minimo prudente per design, non un residuo del bug). Il log auto_bet ora
  recita "adaptive Kelly" (prima "dynamic Kelly").

### Verifica settlement + orfani + copertura leghe (13/09/2026, sera)

**1) ESITO DEL SETTLEMENT IN PRODUZIONE (giro manuale + automatico).**
  Previsioni aperte **107 → 92**, bet aperte **2 → 1** (l'unica rimasta e' una
  live di Serie A con kickoff posticipato), 139 risultati scaricati (39 nuovi
  in `match_results`), costo **24 crediti** (410 → 386). Il settlement
  automatico gira: `sx_signals_job` ogni 15' (percorso SX-native, gratis) +
  watchdog. **0 delle 92 righe aperte ha un risultato in `match_results`**:
  nessun disallineamento, il residuo e' tutto spiegato — 21 righe orfane +
  1 OU senza riga in `matches`, ~34 in leghe fuori dal catalogo
  the-odds-api, il resto partite future o oltre la finestra di 3 giorni.
  ⚠️ Nota operativa: `sx_signals_job` puo' durare ~4-5 minuti e in quella
  finestra fa scattare `auto_bet_job skipped: max running instances (1)`
  (anti-sovrapposizione voluta; il giro si riprende al minuto dopo).

**2) RIGHE ORFANE: NON RECUPERABILI, E ORA SCADONO TUTTE.** Le 21 righe
  `sx-*` senza riga in `matches` (batch 09/09 e 10/09) **non hanno market_hash
  sul ledger** (nessuna bet associata) ne' nomi squadra: `markets/find`
  richiede gli hash e `markets/active` non le elenca piu' (eventi conclusi),
  quindi **nessuna fonte puo' piu' saldarle**. Erano gia' coperte dal ramo
  orfani di `expire_stale_sx_rows` (chiusura come push dopo `SX_STALE_DAYS`).
  **Estensione 13/09**: il ramo orfani non e' piu' limitato a `sx-%` —
  senza riga in `matches` mancano kickoff E nomi, quindi la riga e'
  insaldabile con QUALSIASI prefisso (caso reale: la previsione OU
  `6185eb4f...` del 01/09, rimasta senza partita e aperta da 12 giorni).
  Le righe CON riga in `matches` restano intatte (refertabili, le chiude il
  settle vero): test dedicati `test_orfana_non_sx_senza_matches_scade` e
  `test_orfana_non_sx_con_match_non_scade` in test_sx_native_settlement.py.

**3) COPERTURA LEGHE SCOPERte — VERIFICATO: NON AGGIUNGIBILI.** Chiamata
  REALE a `/v4/sports` (costo **0 crediti**: l'endpoint non consuma quota,
  remaining invariato a 386): 84 sport totali, 49 di calcio. **Colombia:
  ASSENTE**; **Korea: solo `soccer_korea_kleague1`** (nessuna K League 2);
  **Argentina: solo `soccer_argentina_primera_division`** (nessuna Nacional).
  Quindi `Primera A`, `Primera Nacional`, `K2-League` non sono copribili —
  non e' una scelta di crediti, la competizione non esiste nel catalogo.
  Confermata la nota 12/09: **NON aggiungerle a `SPORTS_MAP`**. Le altre
  label non mappate (`Division Profesional`, `LigaPro`, `First League`)
  sono ambigue senza corrispondenza univoca (il resolver torna None per
  prudenza: mai indovinare la competizione). Per queste leghe l'unico
  percorso di settlement e' SX-native, e funziona solo per le righe con
  market_hash sul ledger.

**4) ETICHETTA LOG CORRETTA.** `auto_bet` stampava "strategia FREQUENZA
  alta" (residuo del commit 13/09): ora dice "strategia favoriti netti
  (EV_MIN=2%, ODDS 1.30-1.80, edge >= +3pp, adaptive Kelly)", con i valori
  presi dalle costanti reali — cosi' un operatore che legge i log non puo'
  credere attiva una strategia che non c'e' piu'.

### Costo reale del settlement + diagnosi del residuo (13/09/2026, notte)

**1) MISURATO: il settlement costava ~28 leghe/giorno, di cui META' per pura
  verifica.** Un giro manuale reale e' costato **24 crediti** (410 → 386) —
  valore in eccesso, perche' nella stessa finestra giravano anche gli altri
  job. Il pianificatore sul DB di produzione (13/09) dice: **28 leghe
  pianificate**, di cui **14 con righe aperte** (7 delle quali NON mappate →
  saltate a costo 0) e **14 che entravano SOLO per la finestra di
  verifica/heal** (tutte le grandi: PL, Serie A, La Liga, Bundesliga,
  Ligue 1, Eredivisie, Brasileirao, CL, EL, Championship, Serie B,
  Allsvenskan, Belgio, Brazil B). Poiche' la cache punteggi ha TTL 24h,
  quelle 14 venivano riscaricate ogni giorno per ri-verificare righe GIA'
  CHIUSE: era la voce di costo principale. Le leghe NON mappate
  (`Primera A`, `Nacional`, `K2-League`, `Division Profesional`, `LigaPro`,
  `First League`) erano gia' saltate PRIMA della chiamata → costo 0,
  nessuno spreco da tagliare li'.

**2) DUE LEVE DI RISPARMIO (env, zero redeploy di codice).**
  - **Finestra di refertazione allineata alla API** (`SETTLEMENT_WINDOW_DAYS`,
    default `3` = `odds_api.SCORES_DAYS_FROM`): il pianificatore usava **5**
    giorni mentre `/scores` copre **3**, quindi una lega le cui uniche righe
    aperte stavano a 3-5 giorni veniva interrogata (PAGATA) senza poter
    saldare nulla. Ora `_settlement_window_days()` li tiene allineati e un
    tripwire lo verifica.
  - **Verifica periodica invece che a ogni scadenza cache**
    (`SETTLEMENT_HEAL_INTERVAL_HOURS`, default **36**): una lega SENZA righe
    aperte viene ri-interrogata solo se la sua cache punteggi e' piu'
    vecchia dell'intervallo (prima: ogni 24h, cioe' ogni giorno). Con 0 si
    torna al comportamento pre-13/09. Le leghe **con righe aperte si
    interrogano sempre**, con cache fresca o no: il risultato serve per
    saldare.
  Sui numeri del 13/09: la popolazione di verifica passa da 1 fetch/24h a
  1/36h (-33% su 14 leghe) → costo atteso da ~21 a ~16 crediti/giorno.

**3) IL COSTO ORA E' MISURABILE E LEGGIBILE A COLPO D'OCCHIO.**
  - **Log per giro** (`bot._update_results`): `settlement: N leghe
    interrogate (M non mappate, saltate), K partite aggiornate, crediti
    X -> Y (D usati)` — prima il consumo del settlement non era misurato da
    nessuna parte (si leggeva solo il contatore globale).
  - **`tracker.settlement_residue()`** rompe le righe aperte per MOTIVO:
    `no_match_row` (insaldabile: niente kickoff ne' nomi), `league_unmapped`
    (fuori catalogo/etichetta ambigua), `out_of_window` (partita piu' vecchia
    della finestra /scores), `not_started` (futura), `awaiting_result`
    (refertabile, risultato non ancora arrivato). Aggiunge il costo atteso
    del prossimo giro (`estimated_credits`, conta solo le leghe mappate con
    cache scaduta), lo split `cost_open_driven` / `cost_heal_only`, il
    pianificatore (`leagues_to_query`) e **`overdue_orphans`**.
  - **Esposto in `GET /api/health`** (campo `settlement`): il residuo non e'
    piu' un numero opaco.
  - **`overdue_orphans` DEVE essere 0**: sono righe insaldabili oltre la
    soglia di scadenza. Se sale, la scadenza automatica (`expire_stale_sx_rows`)
    non sta girando. **E' il controllo automatico dei 21 orfani `sx-*`**: la
    verifica "dopo il 15/09" e' che questo campo resti **0** e che le righe
    del batch 09/09 (18 righe, scadenza 14/09 23:03 UTC) e 10/09 (3 righe,
    15/09 18:30 UTC) non siano piu' aperte.
  Test: `test_settlement_watchdog.py` (`TestRisparmioCreditiSettlement`,
  `TestResiduoSettlement` — allineamento finestra, controprova che la vecchia
  finestra a 5gg le interrogava, verifica periodica vs cache fresca, lega con
  righe aperte sempre interrogata, classificazione dei motivi, costo atteso
  con cache fresca/scaduta, residuo vuoto).

### Workflow agentico di ricerca su grafo (14/09/2026, `research_graph/`)

Modulo NUOVO e INDIPENDENTE (non tocca tracker/bot/produzione, zero rete):
workflow di ricerca a grafo con validazione IBRIDA (deterministica + semantica)
e ciclo di retry con feedback strutturato, cap a **3 attempt**. Nasce come
infrastruttura riutilizzabile per la ricerca (strategie 2026, CLV, devigging).

- **Struttura**: `models.py` (schema Pydantic + stato), `providers.py`
  (contratti `SearchTool`/`Validator` + Mock), `nodes.py` (i 4 nodi + terminali),
  `graph.py` (mini StateGraph + wiring), `__main__.py` (demo con i mock).
  Dipendenza nuova: **pydantic >= 2.0** (aggiunta a requirements.txt).
- **Stato centrale** (`ResearchState`): `findings` (validati, cumulativi),
  `raw_findings` (buffer del SOLO attempt corrente), `validation_feedback`,
  `attempt`, piu' `attempts_log`, `node_trace`, `rejected`.
- **Research Agent**: 1o giro = query generale; sui retry legge il
  `validation_feedback` e genera query MIRATE (`queries_for_retry`: query
  suggerite -> lacune -> problemi -> fallback deterministico), mai duplicati
  (`state.add_query`). Errori del search tool catturati (fail-safe).
- **Controlli deterministici (Pydantic)**: valida il buffer dell'attempt con
  `Finding.model_validate` (campi obbligatori, confidence in [0,1], evidenza
  >= 20 char), dedup su (claim, fonte) e minimo `MIN_FINDINGS` (2). Un payload
  non conforme NON arriva mai all'LLM e produce un fail con indici/campi
  respinti. I respinti non vengono ri-validati (un finding malformato non
  avvelena i retry) e un retry senza query nuove non azzera il materiale gia'
  raccolto.
- **LLM Validator (semantico)**: riceve `ValidationRequest` (query, claim
  richiesti, findings validati, attempt) e risponde `pass`/`fail` + feedback;
  le uscite `dict`/stringa sono normalizzate con Pydantic (`coerce_verdict`),
  un errore dell'adapter diventa un fail con feedback (fail-closed).
- **Decision node**: `pass` -> finalize, `fail` -> retry, `fail` con
  `attempt >= max_attempts` -> **hard_stop** (il cap sta nel router, non nei
  nodi). Il verdetto `fail` SENZA feedback e' rifiutato alla costruzione:
  il retry non puo' restare senza istruzioni.
- **Engine**: `StateGraph` con edge semplici/condizionali, validazione del
  wiring in `compile()` (nodo senza uscite, target inesistente, uscita
  doppia) e tetto di nodi eseguiti (`max_steps`): un ciclo non protetto
  SOLLEVA `GraphError` invece di girare. `run_research` e' fail-safe (errori
  imprevisti -> `status="error"`, mai eccezioni al chiamante).
- **Mock-first**: `MockSearchTool` (dict/callable/batch a copione, registra
  `calls`) e `MockLLMValidator` (copione di verdetti, `requests`, default per
  i casi ripetuti). Gli adapter reali si sostituiscono in
  `build_research_graph(search=..., validator=...)` **senza toccare nodi ed
  edge** (verificato da un test con adapter custom).
- **Test** (`test_research_graph.py`, 40 verdi): i tre scenari richiesti
  (stop immediato al 1o attempt; retry con feedback che guida query mirate e
  passa al 2o; hard stop a 3 attempt con validator chiamato esattamente 3
  volte) + schema Pydantic, dedup, retry senza query nuove, agnosticismo
  provider, guardie dell'engine e fail-safe.
- Demo: `venv/bin/python -m research_graph [retry|hard-stop|schema|all]`
  (tutto sui mock: nessuna rete, nessun credito, nessun ordine).

**Passo 1 — adapter di ricerca REALE (Exa)** (default `type: "neural"` dal
14/09: e' l'unico tipo che restituisce `score`, vedi sotto) (`research_graph/exa_search.py`).
Il contratto `SearchTool` implementato con ricerca web vera, zero dipendenze
nuove (`requests` e' gia' nel progetto): `POST https://api.exa.ai/search`,
auth `Bearer` con `EXA_API_KEY`, `contents: {highlights, text}`. Scelto con la
Gravity Index perche' e' un motore pensato per agenti (risultati citabili;
`highlights` = estratti dei soli token rilevanti, il materiale ideale per
`Finding.evidence`). Mapping: `claim <- title`, `evidence <- highlights`
(fallback testo troncato), `source <- url`, `confidence <- score` se in [0,1]
altrimenti 0.5. FAIL-CLOSED senza chiave (`ExaSearchError`, nessuna ricerca
inventata), errori HTTP/JSON -> eccezione (il nodo la registra, il cap degli
attempt fa da rete), risposta senza `results` -> lista vuota (nessuna evidenza:
fail deterministico -> retry mirato). I risultati senza titolo ne' url vengono
scartati. Chiave SOLO da env o iniettata; `http_post` iniettabile -> test
OFFLINE (trasporto finto). Verifica manuale reale:
`venv/bin/python -m research_graph live "<query>"` (richiede `EXA_API_KEY`).

**Passo 2 — persistenza del trace** (`research_graph/trace_store.py`).
Ogni run (passato, hard stop o errore) lascia una riga JSONL append-only sul
volume (`RESEARCH_TRACE_DIR`, default `DATA_DIR/research`): query, claim
richiesti, status, attempt, findings/respinti, query usate, feedback di ogni
giro, node_trace. `run_research(..., trace_store=TraceStore(path))` — la
persistenza sta FUORI dal grafo (il workflow non cambia) ed e' FAIL-SAFE: una
scrittura fallita finisce in `record["error"]` e non rompe mai la ricerca.
Lettura/riepilogo: `iter_traces` (piu' recenti prima, righe corrotte ignorate,
limite/finestra), `summary` (run, pass rate, attempt medi, tipi di feedback,
top blocker), `format_report` (Telegram-friendly). CLI:
`venv/bin/python -m research_graph.trace_store [--limit N] [--days N] [--json]`.
La demo supporta `--trace [--trace-path PATH]`.

**Passo 3 — adapter LLM REALE (Gemini)** (`research_graph/gemini_validator.py`).
Il contratto `Validator` implementato con `google-genai` (gia' nel progetto,
pattern di `ai_commander.py`), JSON mode e `GOOGLE_API_KEY` SOLO da env.
`build_prompt` e' deterministico (query, claim numerati, ogni evidenza con
fonte e confidenza) cosi' e' ispezionabile e testabile senza rete; il parsing
(`extract_json` + `verdict_from_data`) e' difensivo: accetta code fence e testo
attorno, schema piatto o annidato, normalizza i campi; una risposta
ILLEGGIBILE non diventa mai un pass ma un fail con feedback (fail-closed),
mentre un errore di trasporto solleva e il nodo lo converte in fail. Client
iniettabile -> test offline con client finto; chiave mai nel prompt (tripwire
dedicato). `RESEARCH_LLM_MODEL` per cambiare modello (default
`gemini-3.6-flash`, come `ai_commander`).

**Test**: `test_research_graph.py` (42), `test_research_exa.py` (22),
`test_research_trace.py` (17), `test_research_gemini.py` (26) — **107 verdi
offline** (`-m "not integration"`) e **109 verdi** con i due `integration`
attivi (rete + quota, si accendono da soli quando `EXA_API_KEY` e
`GOOGLE_API_KEY` sono configurate). Nessuna chiave in chiaro nei sorgenti
(il tripwire `test_secret_hygiene.py` resta verde: le credenziali finte dei
test sono marcate `fake/`).

**Chiavi reali in esercizio (14/09/2026)** — gli adapter vedono davvero
`EXA_API_KEY` e `GOOGLE_API_KEY`: verifica LIVE end-to-end
`venv/bin/python -m research_graph live "<query>"` -> 5 pagine Exa, Gemini
reale, verdetto `pass` al 1o attempt, `pagine raccolte: 5`; i due test
`integration` passano (`pytest -m integration`: 2 verdi).
⚠️ Due trappole verificate sul campo (da non ripetere):
1) il progetto legge SOLO il `.env` nella ROOT del progetto
   (`config.load_dotenv` usa il path del modulo, NON `find_dotenv`): una
   chiave scritta in `$HOME/.env` NON viene mai letta — la variabile risulta
   assente e l'adapter resta fail-closed. Verifica rapida senza stampare
   valori: `exa_configured()` / `gemini_configured()`.
2) `secrets_store.py vault --commit` NON fa merge: ricostruisce `vault.bin`
   dai SOLI file plaintext presenti in `secrets/`, quindi CANCELLA i segreti
   gia' cifrati (qui ne avrebbe distrutti 3 su 4). Per aggiungere un segreto:
   merge esplicito (`load_vault()` -> aggiungi la voce -> risecrittura atomica
   con `_fernet_from_master(_master_key())`, chmod 600) oppure mettere in
   `secrets/` i file plaintext di TUTTE le voci prima del commit. `EXA_API_KEY`
   e' entrata con il merge: il vault ora ha 5 segreti (API_FOOTBALL_KEY,
   EXA_API_KEY, GITHUB_TOKEN, GOOGLE_API_KEY, QUOTAVERACE_BOT_TOKEN).
   Backup del vault pre-modifica lasciato in `/tmp` (mai in `secrets/`: i
   file non in `_SKIP_NAMES` verrebbero letti come plaintext e finirebbero in
   `os.environ`).
Con le chiavi presenti i due `integration` NON si saltano piu' nella suite
completa (usano rete e quota): per un giro offline `-m "not integration"`.

**Exa: default `neural` per avere confidence reali (14/09).** Il campo `score`
(mappato su `Finding.confidence`) arriva SOLO con `type: "neural"`: col tipo
`auto` il payload non lo contiene (verificato chiamando l'API: i campi del
risultato sono favicon/highlights/id/image/publishedDate/text/title/url) e la
confidence restava sempre `DEFAULT_CONFIDENCE` (0.5). Ora il default e'
`neural` (`DEFAULT_SEARCH_TYPE`, override con l'env `EXA_SEARCH_TYPE`; nuovo
`ExaSearchTool.resolved_search_type()`), e nella verifica live le confidence
sono reali: 1.00 / 0.75 / 0.50 / 0.25 / 0.00 (Exa normalizza lo score sul set
di risultati: l'ultimo puo' valere 0.0, ed e' accettato da `Finding`, che
richiede solo 0 <= confidence <= 1). Per tornare al vecchio comportamento
senza score: `search_type="auto"` (o `EXA_SEARCH_TYPE=auto`); con
`search_type=""` il campo `type` viene omesso. Test: classe
`TestTipoDiRicerca` in `test_research_exa.py` (default neural, env che cambia
il default, esplicito che vince sull'env, tipo vuoto, score -> confidence).

**Chiavi in produzione su Railway (14/09).** `EXA_API_KEY` e `GOOGLE_API_KEY`
sono state impostate sul servizio `api` (ambiente production), lette dal VAULT
e passate via stdin (`venv/bin/python secrets_store.py get NOME | railway
variable set NOME --stdin --service api`): il valore non e' mai transitato in
chat. Verificato SUL CONTAINER (dopo redeploy SUCCESS): `EXA_API_KEY` len 36
sha12 `77620c539824`, `GOOGLE_API_KEY` len 53 sha12 `23099b012e46` (le stesse
impronte di vault e locale), `/api/health` 200. Le due voci sono dichiarate in
`.railway/railway.ts` (`researchEnv`, SOLO sul servizio `api`: il cron surebet
non ne ha bisogno) insieme a `EXA_SEARCH_TYPE`, `RESEARCH_LLM_MODEL`,
`RESEARCH_TRACE_DIR`, cosi' `railway config apply` non le distrugge.
⚠️ Da sapere:
- `research_graph/` NON e' ancora deployato (i moduli sono uncommitted):
  sul container le env ci sono ma `import research_graph` da'
  `ModuleNotFoundError` finche' il codice non viene push-ato su `main`.
- C'erano DUE chiavi Google diverse (len 53 entrambe): la shell/`~/.env`
  (`23099b012e46`) vinceva sulla vault (`f03990738182`) perche' `config` usa
  `os.environ.setdefault`. Entrambe valide (verificate con una chiamata
  Gemini reale), ma la doppia fonte era ambigua: il vault e' stato allineato
  a `~/.env` e Railway usa ora quella, quindi vault = `~/.env` = Railway.
- La riga `EXA_API_KEY` duplicata in `$HOME/.env` e' stata rimossa (backup in
  `/tmp/home-env.bak-*`): il progetto la legge dal vault.
- `railway config plan` FUNZIONA: la CLI 5.54.1 valuta il `.railway/railway.ts`
  con l'SDK `railway@3.11.0` di `/home/siryo/node_modules`, che e' esattamente
  l'ultima versione pubblicata (`npm view railway version` → 3.11.0). Nessun
  pacchetto obsoleto da sistemare.
  ⚠️ NON avvolgere il comando in un wrapper (`timeout 120 railway config plan`):
  il check di compatibilita' dell'SDK (`assertMinimumIacCliVersion`) esegue
  `$process.env._ --version` e pretende una terna x.y.z ≥ 5.42.1. Con `$_` =
  `timeout` (`timeout (GNU coreutils) 8.32`) il check fallisce con
  "This version of railway/iac requires Railway CLI 5.42.1 or newer": e' un
  FALSO allarme, non c'entra col file IaC. Lanciare il comando nudo.

**Drift IaC chiusa (14/09).** `railway config plan` segnalava 2 variabili
DISTRUTTIVE (presenti su Railway ma non dichiarate nel file):
`api.TENNIS_SANDBOX_ENABLED` — un `config apply` avrebbe SPENTO il sandbox
tennis (scan+settle) — e `surebet.SUREBET_CRON_HOLD_SECONDS`; in piu'
`surebet deploy.restartPolicyType` ("NEVER" → null: un apply avrebbe rimesso il
restart su errore su un container-cron che DEVE uscire a fine scan). Corretto
dichiarando con `preserve()` le variabili (`TENNIS_SANDBOX_ENABLED` piu' i limiti
di staking tennis, `SUREBET_CRON_HOLD_SECONDS` solo sul cron) e
`restartPolicyType: "NEVER"` nel blocco `deploy` del cron. Ora il piano e'
**"0 to add, 1 to change, 0 to destroy"** (l'unico cambio e' il flag
`config.isCreated` di api-volume, non distruttivo e gia' dichiarato nel file).
Regola: dopo ogni modifica al file IaC o alle env da dashboard, girare
`railway config plan` e pretendere **0 to destroy**.

### Gerarchia a 4 Agenti + Chief Orchestrator + AdvisorAgent (27/09/2026, Fase 1 SHADOW)

Direttiva del proprietario: modello piramidale **Chief → 4 Lead Agent → sub-agenti**, con
l'esecuzione reale del denaro INALTERATA (Fase 1 = solo shadow) e orchestrazione
headless (job APScheduler / CLI / HTTP-n8n).

**Nuovi file (tutti wrapper di DELEGA, zero formule copiate):**
- `chief_orchestrator.py` — la state machine del ciclo: Data → Strategy → Finance
  → [Advisor] → Execution. Il requisito gerarchico "esecuzione solo se la Finanza
  approva" e' la mappa comandi di `decision.engine` (place_order solo su approve),
  filtrata nel Capo per difesa in profondita'. UN MarketFeed condiviso fra Data e
  Finance (un refresh per ciclo, stessa identita' di mercato). Fail-safe totale
  (un errore del ciclo = `blocked_reason`, mai un'eccezione al chiamante). CLI:
  `venv/bin/python chief_orchestrator.py [--json]` (exit 1 se bloccato).
- `agents/contracts.py` — contratti Pydantic di uscita per agente (`MarketData`,
  `StrategyOutput`, `FinanceOutput`, `ExecutionOutput`, `CycleReport`,
  `AdvisorResolution`), tutti serializzabili (`as_json()`) per log/HTTP/n8n.
- `agents/data_agent.py` — delega a `iter_signals` + `MarketFeed`/`verify_feed`.
- `agents/strategy_agent.py` — filtra per `PLAYABLE_TIERS` (UNA definizione, da
  `value_filter`); gli stati fuori lista finiscono in `unclassified` (mai spariti).
- `agents/finance_agent.py` — delega a `build_plan` (fail-fast → risk → stake →
  comandi), con `kills`/`feed` iniettabili e `bankroll_override` per l'Advisor.
- `agents/execution_agent.py` — delega a `Dispatcher` + `ShadowGateway` (Fase 1:
  SOLO shadow; il ValidatingLedgerGateway e' opt-in `persist=True`). Tripwire:
  nessun `PlaceOrderGateway`/`execution_engine` montabile.
- `agents/advisor_agent.py` — il **braccio destro** del Capo: interpellato SOLO su
  piani bloccati (reject di valore o stake non eseguibile — nota: `LIQUIDITY_LOW`
  e' un motivo di STAKE con verdetto approve, gestito). Tre risoluzioni: (1)
  **micro-stake** = ri-valutazione con lo STESSO motore e bankroll virtuale
  frazionato (0.5x/0.25x): se il piano ridotto diventa approve+eseguibile e'
  l'UNICO override che diventa ordine (sempre via percorso shadow in Fase 1);
  (2) **market switch**: propone il gemello OU/AH GIOCABILE dello stesso evento
  come ESCALATION umana (mai segni calcolati ad hoc); (3) **contesto**: rule
  engine deterministico (+ LLM opzionale che affina solo la NOTA) per falsi
  positivi/steam chasing → SEMPRE escalation alla coda revisioni. Autorita'
  intoccabili (KS, stop loss, feed) e soglie di strategia congelate (EV/edge/
  fascia/lega): non negoziabili — si sale all'umano, non si scavalcano.
  Interruttore `DECISION_ADVISOR` (default ON; `advisor_enabled=False` da codice).

**Perche' queste scelte**: i bug gravi di progetto (stop-loss fantasma 21/09,
doppio Kelly 13/09, doppio ordine 26/09) sono tutti "logica copiata in due
posti"; ogni formula resta nel modulo originale e i wrapper sono facciate
sottili con test di PARITA' (wrapper vs modulo: stessi input → stessi valori).
Il micro-stake usa il bankroll virtuale perche' Kelly/cap sono proporzionali al
bankroll (il floor no: se cade sotto il floor anche al minimo, il blocco e'
strutturale e resta).

**Test**: `test_agent_hierarchy.py` (**17 verdi**: parita' per i 3 wrapper +
tripwire "niente denaro/import leggeri" + e2e offline: approve/review-copertura/
reject-fascia/KS-iniettato/gate-mercato/fail-safe/CLI) e `test_advisor_agent.py`
(**13 verdi**: perimetro, micro-stake con LIQUIDITY_LOW reale, blocco strutturale
live, review-gia'-presidiata, market switch solo-escalation, LLM solo-nota,
fail-safe, ramo del Capo con advisor ON/OFF, tripwire gateway shadow,
contratto serializzabile). Regressioni `decision/` 299 verdi.

**⚠️ NON collegato alla produzione dei soldi**: `auto_bet.run_today_bets` resta
l'unico esecutore; il Chief gira come CLI diagnostica e sara' collegato in
shadow a un job solo dopo il push. Fasi successive: F2 = confronto shadow
(`decision_compare`), F3 = cutover (l'Execution Agent monta `PlaceOrderGateway`
verso `auto_bet._live_fill` e `run_today_bets` smette di orchestrare).

### Fase 2 avviata — Task 2.1: ciclo del Chief nel giro di produzione, SHADOW (27/09/2026, notte)

Direttiva del proprietario: piano di uscita dalla shadow in 5 task (2.1 wiring shadow,
2.2 raccolta dati + CLI confronto, 2.3 criteri di sblocco SCRITTI PRIMA di guardare
i numeri: >= 7 giorni, >= 50 decisioni, 0 divergenze sugli invarianti, feed validato
>= 95% dei cicli, micro-stake mai sopra cap; 2.4 canary 1 ordine/giorno a floor
1 USDC in finestra T-60 con `CHIEF_EXECUTION=canary`; 2.5/F3 cutover).

**Task 2.1 COMPLETATO** (`chief_shadow_wiring.py` + hook in `auto_bet._shadow_run`):
- a OGNI giro di `auto_bet` (dopo corsia reale e shadow `decision/`) il
  **ChiefOrchestrator valuta gli stessi segnali** con la piramide (Data ->
  Strategy -> Finance -> Advisor -> Execution) e registra il riepilogo in
  `data/decision/chief_cycles.jsonl` (env `CHIEF_CYCLE_LOG`; default
  `DATA_DIR/decision/chief_cycles.jsonl`);
- **nessun effetto reale**: Execution Agent solo shadow, zero crediti (feed con
  riuso), fail-safe totale: l'hook gira in `finally` con doppia cintura, un
  errore e' una riga DEBUG, mai un'eccezione al giro puntate; interruttore
  `CHIEF_SHADOW_ENABLED` (default ON, `0` per spegnere);
- CLI di lettura: `venv/bin/python chief_shadow_wiring.py [--days N] [--json]`
  (cicli, verdict, blocchi, consigli advisor per kind);
- il FinanceAgent del ciclo usa bankroll/mode del giro reale (same-era, cosi' il
  confronto 2.2 e' apples-to-apples).

**Test**: `test_chief_shadow_wiring.py` (**8 verdi**: hook registra + giro
`run_today_bets` INVARIATO con l'hook attivo (KS off, 0 puntate), fail-safe su
modulo rotto, interruttore, summarize/report coerenti, path default, tripwire
"nessun gateway reale" + import leggero in subprocess). Due bug reali fixati in
fase di test: `FeedSnapshot` non ha `as_json` (usato `model_dump(mode="json")`
in `MarketData.as_json` — nei test passava perche' il conftest spegne il feed) e
una variabile rimasta orfana nel refactoring di `summarize`.
**Tripwire preesistente riparato**: `test_decision_feed.test_regola_dichiarata_
nella_catena` falliva GIA' senza le mie modifiche (verificato con `git stash`):
la catena `SAFETY_CHAIN` ha ora 4 regole (weekly_stop aggiunto col CB settimanale
26/09) e il test aspettava 3 — allineato.
Regressioni verdi in lotti: gerarchia+advisor+wiring (38), auto_bet+pipeline+
commands (144), guards/shadow/validation/adapters/limits/feed/t60/weekly (191),
review/review_telegram/compare/clv (85). `compileall` OK, 0 marker.

**Prossimo: Task 2.2** — lasciar registrare i cicli sul container 3-7 giorni e
leggere `chief_shadow_wiring.py` per il confronto Chief <-> corsia (agreement,
blocchi, consigli advisor). Il canary (2.4) parte SOLO dopo i criteri del 2.3
con via libera esplicita del proprietario.

### Catena di decisione `decision/` (14/09/2026)

Direttiva del proprietario: dare alla pipeline una struttura esplicita
Signal → Risk → Stake, con kill switch come autorita' superiore e un feedback
engine che registra tutto. **Decisioni prese**: (1) nuovo pacchetto `decision/`
SOPRA i moduli esistenti (niente riscrittura di `auto_bet`); (2) il verdetto
`review` = coda + approvazione umana su Telegram; (3) precedenza blocchi:
**kill switch manuale > stop-loss giornaliero > pausa settlement**.

**Perche' un orchestratore e non un motore in piu'**: la catena riprende
moduli che esistono gia' (Signal = `sx_signals`/`fixture_engine` + `value_filter`
e `market_calib`; Risk = i gate + i cap di `auto_bet`; Stake =
`adaptive_staking`; Kill switch = `auto_bet_mode.json`/`daily_stop.json`/
`settlement_paused`; Feedback = ledger `tracker` + `ml_ensemble` + `drift_monitor`),
ma **ogni stadio ha un contratto** e le soglie vivono in un solo posto.

**File**:
- `decision/models.py` — contratti Pydantic: `Signal` (con `DataQuality`),
  `RiskDecision`, `StakeDecision`, `KillSwitchStatus`, `DecisionRecord`
  (`as_row()` = riga piatta per il feedback engine) e `ReasonCode`
  (motivi machine-readable, mai prosa). `KILL_SWITCH_PRECEDENCE` e' la
  precedenza decisa dal proprietario.
- `decision/limits.py` — `RiskLimits.from_env()`: **nessun default copiato a
  mano**, i valori si LEGGONO da `value_filter`/`market_calib`/
  `adaptive_staking` (e dagli stessi env di produzione). `required_depth()`
  riproduce max(stake x 2.0, 25 USDC).
- `decision/kill_switch.py` — istantanea dei blocchi con sonde iniettabili.
  Fail-safe in direzioni OPPOSTE e volute: modalita' illeggibile -> `off`
  (fail-closed), stop-loss illeggibile -> non attivo (fail-open). La pausa
  settlement NON blocca la bet: e' un `advisory` (blocca il referto/feedback).
- `decision/risk_engine.py` — i gate in ordine (`kill_switch` per PRIMO) con
  verdetto `approve`/`review`/`reject`; `tighten()` puo' solo ridurre un cap.
  Include il gate di **qualita' dei dati**: copertura del modello sotto
  `min_model_coverage` (default 0.5, env `DECISION_MIN_MODEL_COVERAGE`) ->
  `review` con `DATA_QUALITY_LOW`. Un modello cieco (nessun rating, profilo
  NEUTRO di lega) oggi arriverebbe a un ordine automatico; nella catena decide
  un umano. ⚠️ Trovato scrivendo i test: la sola confidenza NON bastava
  (copertura 0 + calibrato + edge forte = 0.60, sopra la soglia di review).
- `decision/stake_engine.py` — Kelly frazionato scalato **UNA VOLTA** (blinda
  il bug del 13/09 del doppio scaling), tre cap (tier/lega/risk: vince il piu'
  stretto), cap severo fail-closed sotto il floor, controllo di liquidita'
  relativo allo stake (max(stake x 2, 25 USDC)).
- `decision/review_queue.py` — coda persistente su volume
  (`DATA_DIR/decision/reviews.json`, env `DECISION_REVIEW_QUEUE`), scrittura
  atomica, scadenza automatica al KICKOFF (mai approvare a partita iniziata),
  idempotenza, lettura fail-safe (file corrotto -> coda vuota, non sovrascritto).
- `decision/pipeline.py` — ordine fisso: kill switch -> risk -> [revisione] ->
  stake; nessuno stake senza `approve`; `resolve_review()` chiude le revisioni
  umane (approva+dimensiona, oppure rifiuta, oppure `REVIEW_EXPIRED`).
- `decision/adapters.py` — **Signal Engine sui dati reali**: legge il ledger
  (`matches` JOIN `predictions` LEFT JOIN `match_analysis`) con la STESSA
  selezione di `auto_bet._today_value_picks` (status value/strong_value/
  moderate, mercato 1X2, `esito_finale IS NULL`, finestra mobile 24h, ORDER BY
  ev DESC) e produce i `Signal`. `model_prob` da `match_analysis.prob_1/X/2`
  (pre-blend; se assente -> blend + warning `model_prob_assente`), `tier` dallo
  `status` del ledger, copertura da `team_ratings` (`n_home`+`n_away`,
  `model_coverage()` lineare con campione pieno a 8 partite), `confidence` da
  `compute_confidence()` (pesi ESPLICITI in testa al file: gate superato 0.35,
  copertura 0.30, calibrato 0.15, edge forte 0.10, libro profondo 0.10, CLV
  ±0.05 — euristica dichiarata, da ricalibrare sul ledger).
  **Sola lettura** (tripwire: nessun INSERT/UPDATE/DELETE nel sorgente) e
  NIENTE rifiltro delle quote: la difesa in profondita' che in `auto_bet`
  scartava in silenzio qui diventa un `ReasonCode` contabilizzabile
  (`ODDS_TOO_HIGH`, `NOT_FAVOURITE`...). `conn`, `resolve` (nomi squadra) e
  `depth_lookup` sono iniettabili: i test girano su un SQLite temporaneo con lo
  schema di produzione. CLI: `venv/bin/python -m decision queue`.
- CLI: `venv/bin/python -m decision demo [--bankroll N] [--mode live|sim|off]`
  (tre scenari approva/review/reject, offline) e `... -m decision queue [--json]`.

**Regole garantite da tripwire**: il `Signal` non contiene stakeholder ne'
bankroll; il Risk Engine puo' solo stringere; il kill switch risponde prima di
qualunque calcolo; `import decision` non carica `auto_bet`/`bot`/`tracker`.

**Parita' con la produzione**: `test_decision_pipeline.py` confronta su una
GRIGLIA di 54 casi il set dei `reject` del Risk Engine con i rifiuti di
`value_filter.is_sane` — devono coincidere esattamente, cosi' il nuovo percorso
non puo' divergere dal vecchio senza che un test lo dica.

**Drift resa VISIBILE (non corretta)**: `adaptive_staking.MAX_STAKE_PCT` (env
`STAKE_CAP_PCT`, **1%**) e' il cap che il BOT applica, mentre
`value_filter.MAX_STAKE_PCT` (**2%**, commento "era 1%") e' quello mostrato dai
tool schedina/`/value`. `RiskLimits` tiene entrambi (`cap_value`/`cap_display`)
e `test_decision_limits.py` verifica che ciascuno segua la PROPRIA fonte: la
divergenza resta visibile invece di nascondersi dietro un numero solo. Da
decidere se allineare (e' una scelta di strategia, non un bug tecnico).

**Test**: `test_decision_pipeline.py` (49), `test_decision_adapters.py` (20),
`test_decision_review.py` (18), `test_decision_limits.py` (18) — **105 verdi,
tutti OFFLINE** (nessun DB di produzione, nessuna rete, nessun provider: DB
SQLite temporaneo e sonde/engine iniettati). Il test e2e
`TestCatenaSuDatiReali` porta un ledger temporaneo da riga a `Signal` a
verdetto: approve (rating pieni) / review (modello cieco) / reject (quota
fuori fascia).

⚠️ **NON e' collegato alla produzione**: `auto_bet` continua a usare il
percorso attuale, quindi nessun ordine cambia finche' non si decide di
sostituirlo. Prossimi passi: (1) persistenza del `DecisionRecord` per il
feedback engine (tabella `decisions` in `tracker.py`, migrazione idempotente);
(2) approvazione Telegram sulla coda (bottoni sulla voce di `ReviewQueue`);
(3) collegamento in `auto_bet` con la parita' dei gate come rete di sicurezza.

#### Passo 1 FATTO — persistenza del `DecisionRecord` + feedback engine (14/09/2026)

La catena ora **lascia una traccia**: ogni `DecisionRecord` si scrive sul
ledger `decisions` e la telemetria e' leggibile.

- **`tracker.decisions`** (nuova tabella, migrazione idempotente
  `_ensure_decisions_table`): una riga per decisione con input (mercato,
  quota, prob modello/blend, copertura, calibrazione), verdetto + motivo
  (`ReasonCode`), stake/cap applicati, revisione umana e poi ordine ed esito.
  Colonne "late" (`order_id`, `order_status`, `esito_finale`, `profit`,
  `settled_at`) in UPDATE usano `COALESCE`: ripersistere lo stesso record
  **non cancella** ordine ed esito.
  ⚠️ **L'ordine di `_ensure_decisions_table` conta** (tabella -> colonne ->
  indici): creando gli indici prima della migrazione delle colonne,
  `_get_conn` falliva all'avvio su un `decisions` vecchio/parziale — cioe'
  avrebbe messo giu' il BOT al primo deploy su un DB gia' migrato. Trovato
  dai test (`test_migrazione_idempotente_su_tabella_vecchia`).
- **API tracker**: `save_decision(record)` (idempotente su `record_id`),
  `get_decisions(closed/verdict/limit)`, `update_decision_order(record_id,
  order)`, `settle_decisions()` (chiude **anche** `review`/`reject`: senza
  l'esito degli scartati non si misura il gate; `profit` = P/L **per unita'
  di stake**, il peso lo da' la colonna `stake`; rispetto di pausa settlement
  e sanity check come gli altri ledger), `decision_stats()`.
- **`decision/feedback.py`** (nuovo): il lato PERSISTENZA, **fuori** dalla
  pipeline (che resta orchestratore puro, senza DB — stesso schema di
  `TraceStore` in `research_graph`). `persist()`, `persist_many()`,
  `attach_order()`, `settle()`, `stats()`, `snapshot()`, `format_report()`.
  **Scritture fail-safe** (mai un'eccezione: la telemetria non deve fermare
  una puntata, ritorna `{saved, error}` e logga) e **letture read-only**.
  `store` iniettabile → i test girano su un ledger finto. Import di `tracker`
  PIGRO dentro le funzioni: il tripwire "`import decision` non carica la
  produzione" resta verde (`test_import_feedback_non_carica_tracker`).
- **`DecisionRecord.as_row()`** esteso con `selection_label`, `provider`,
  `approved_by`, `review_note` (era l'unico punto mancante per una riga
  completa).
- **CLI**: `venv/bin/python -m decision feedback [--json] [--settle]`
  (`--settle` e' l'unica scrittura, opt-in).
- **Metrica nuova per il feedback**: `decision_stats()["shadow"]` raggruppa
  per verdetto le decisioni NON giocate e chiuse — cosa sarebbe successo:
  e' il costo (o il risparmio) dei gate. Il `settled` delle giocate riporta
  hit rate, ROI flat, **ROI pesato per stake** e `gap_pp` = pnl realizzato -
  EV atteso (stessa convenzione di `predictions_summary`).

**Test**: `test_decision_feedback.py` (28 verdi, offline, DB temporaneo) +
105 dei quattro file `test_decision_*` e i focus `test_settlement_watchdog`,
`test_sx_native_settlement`, `test_bets`, `test_predictions`, `test_dedup_ml`,
`test_auto_bet*`, `test_web_api`, `test_secret_hygiene` — tutti verdi. Effetto
in produzione: al prossimo deploy nasce la tabella `decisions` (vuota finche'
la catena non viene collegata ad `auto_bet`: passo 3).

### Command pattern + Fail Fast + Observability + shadow mode (15/09/2026)

Quattro direttive del proprietario, una rifattorizzazione: **il motore di
decisione non tocca piu' nulla** — emette comandi leggeri, li fa eseguire a
gateway dedicati, si ferma al primo blocco di sicurezza e racconta tutto in log
JSON strutturati. E' collegata alla produzione in **shadow mode** (nessun
ordine reale cambia).

**Decisioni prese dall'agente su indicazione del proprietario** (chieste prima
di implementare): 1) la **pausa settlement NON blocca la puntata** (ferma
referto e feedback engine, come deciso il 14/09); 2) il collegamento e' in
**shadow mode**, non esecuzione reale (passo 3 completo solo dopo il confronto
misurato); 3) **nessuna fixture di sviluppo**: i dati finti restano confinati
nei test, la CLI continua a leggere il ledger vero.

**1) COMMAND PATTERN** — nuovi moduli, tutti puri (nessun import di
`tracker`/`auto_bet`/`bot` a livello di modulo):
- `decision/commands.py`: `CommandKind` (`persist_decision` | `place_order` |
  `notify_operators`), `Command` (dato serializzabile con `command_id`,
  **`dedup_key` stabile** e `order` = posizione nell'ordine dichiarato
  `COMMAND_ORDER`), payload **tipizzati** (`PlaceOrderPayload`,
  `PersistDecisionPayload`, `NotifyPayload`) validati all'EMISSIONE, e
  `CommandPlan` (record + comandi + eventuale blocco).
- `decision/engine.py`: `build_plan()`/`emit_many()` → **solo comandi**. Ordine
  non negoziabile: fail fast → risk → comandi. Tabella dei comandi per
  verdetto: bloccato → persist+notify (1/giorno); reject → solo persist; review
  → persist+notify; approve → persist (+ place_order SOLO se eseguibile e modo
  `live`). In `sim` NIENTE `place_order`: il ledger delle puntate simulate
  resta di `auto_bet`.
- `decision/gateways.py`: `BaseGateway` (template: tempi + cattura eccezioni,
  `_run()` da implementare), `LedgerGateway` (scrive su `decisions`),
  `PlaceOrderGateway` (DELEGA ad `auto_bet._live_fill`: non reimplementare
  l'esecuzione e' l'unico modo per non farla divergere dalla produzione;
  `dry_run=True` per ispezionare), `NotifyGateway` (POST Telegram diretto,
  `sender` iniettabile), `ShadowGateway` (vedi sotto). Nessun gateway solleva:
  un errore diventa `CommandResult(ok=False)`.
- `decision/dispatcher.py`: UNICO punto di esecuzione. Instrada ogni comando al
  primo gateway che lo gestisce, apre uno **span per comando**, aggrega
  `DispatchReport` (`executed/skipped/duplicated/errors/shadow`). `fail-soft`
  per default (un comando fallito non blocca gli altri: l'audit precede
  l'ordine), `raise_on_error=True` per la semantica fail-fast opposta.

**2) FAIL FAST (`decision/guards.py`)** — catena unica e dichiarata:
`SAFETY_CHAIN` = **manual (kill switch) > daily_stop > settlement_pause**, con
`require_clear(kills, stage=...)` che **solleva `SafetyBlockError`** al primo
blocco attivo (porta il `SafetyBlock` dentro: il chiamante non ricostruisce il
motivo). Gli **stadi** rendono onesta la semantica: `betting` (kill switch,
stop-loss) e `settlement` (pausa). Per lo stadio `betting` la pausa e' un
**avviso** (`advisories()`), non un blocco. Nel motore il fail-fast e' reale:
con un blocco attivo il record esce con **`stake is None`** (nessun calcolo a
valle) e i comandi sono solo persist+notify. Direzioni fail-safe invariate:
modalita' illeggibile → `off` (fail-closed), stop-loss illeggibile → non
attivo (fail-open).

**3) OBSERVABILITY (`decision/middleware.py`)** — eventi JSON con
`request_id` (un giro), `trace_id` (un piano), `span_id` + `parent_span_id`
(ogni passo), **`config_hash`** (impronta sha256 dei `RiskLimits` efficaci: le
soglie cambiano da env, senza impronta due decisioni diverse sembrano uguali).
`Observability.span()` emette `span.start`/`span.end` con `duration_ms` e
`span.error` che **ri-solleva** (il fail-fast non si perde nel logging). Sink
**configurabile** con `DECISION_LOG_SINK`: default JSONL sul volume
(`data/decision/events.jsonl`), `stdout`, `off`, oppure un path. Un sink
iniettato vince sull'env. **Rotazione automatica** oltre `DECISION_LOG_MAX_MB`
(default 5 MB, 2 generazioni) — il job gira ogni 60s e un log senza limite sul
volume non serve a nessuno. `redact()` maschera i campi sensibili. **Mai
un'eccezione**: un sink rotto logga un warning (una volta) e la catena va
avanti.

**4) SHADOW MODE (`decision/shadow.py` + `auto_bet._shadow_run`)** — a ogni
giro `auto_bet` valuta i segnali aperti anche con la catena nuova e **registra
i comandi che emetterebbe**, senza eseguire nulla (`Dispatcher([ShadowGateway])`,
`report.shadow=True`). Tre garanzie: (a) nessun effetto reale — niente ordini,
niente Telegram, e **nessuna riga sul ledger `decisions`** (il job gira ogni
60s: il ledger reale si riempirebbe di duplicati; il registro e' il JSONL
`data/decision/shadow_commands.jsonl`, deduplicato per `dedup_key`, con dedup
che sopravvive al riavvio leggendo la coda del file); (b) **zero crediti API**
(legge solo il ledger locale, `depth_usdc=None` → nessuna lettura di libro;
tripwire nei test che avvelena rete e socket); (c) **fail-safe totale** (una
eccezione torna come `{"error": ...}`, mai verso `auto_bet`). Fail fast anche
qui: con un blocco attivo esce **prima** di interrogare il ledger. Se non c'e'
nessun segnale aperto esce **senza emettere eventi** (1440 giri/giorno di
"nessun segnale" sarebbero solo rumore). Interruttore `DECISION_SHADOW`
(default **attiva**: non esegue nulla). Lettura/report: `decision.shadow_summary()`
+ `venv/bin/python -m decision shadow`.

**5) DATI MOCK NELLO SVILUPPO** — scelta "solo gateway di test": i finti
vivono DENTRO i test (gateway in memoria, `fill`/`sender`/`persist` iniettati),
nessuna fixture su disco, la CLI legge sempre il ledger reale. Nuovo
**`conftest.py`** (autouse): `DECISION_LOG_SINK=off` e `DECISION_SHADOW_LOG`
spostato nella `tmp_path` del test — senza isolamento una sessione di test
lasciava **1051 eventi** in `data/decision/` (osservato durante il lavoro).
Verifica end-to-end manuale (isolata con `QUOTAVERACE_DATA_DIR`, attenzione:
NON e' `DATA_DIR`): 1 segnale → approve, comandi `persist_decision` +
`place_order` (`would_order: true`, stake 20.0), ledger `decisions` **vuoto**.

**CLI nuova**: `venv/bin/python -m decision status [--json]` (istantanea del
fail-fast: catena, blocchi per stadio, avvisi, ripresa) e `... shadow [--json]
[--limit N]` (registro shadow).

**Bug reali trovati scrivendo i test** (tutti fixati): 1) `Observability.span`
passava `name=` a `event()`, che ha gia' un parametro `name` → `TypeError` su
OGNI span (il campo ora e' `span_name`); 2) `ShadowGateway` non dichiarava
`dry_run`, quindi `DispatchReport.shadow` era sempre `False` (bug silenzioso:
la shadow non si sarebbe distinta dall'esecuzione); 3) `GatewayError`
costruito dai soli `CommandResult` perdeva i motivi senza risultato (comando
senza gateway) → ora porta le stringhe d'errore; 4) `record_id` ha granularita'
al SECONDO, quindi la `dedup_key` dell'ordine deve dipendere dalla partita, non
dal record (un job ogni 60s avrebbe generato ordini duplicati) — test dedicato.

**Test**: `test_decision_commands.py` (23), `test_decision_guards.py` (19),
`test_decision_observability.py` (41), `test_decision_shadow.py` (22) — **105
nuovi, tutti OFFLINE**; l'intero pacchetto `decision` = **236 verdi** (92s).
Focus regressioni verdi:
`test_auto_bet*`, `test_bot`, `test_settlement_pause`, `test_secret_hygiene`,
`test_web_api`, `test_reports`.

### Contratto di mercato `decision/market.py` (15/09/2026)

**Perche'**: finora una quota entrava nel sistema come **dict anonimo** —
chiavi diverse per provider, tipi non controllati, timestamp a volte senza
fuso orario (la classe di bug che a valle costa: CLV su un istante ambiguo, il
punteggio di una partita in corso usato come finale). Ora la quota e' un TIPO:
`MarketQuote`.

**Campi obbligatori (tutti richiesti, nessun default silenzioso)**:
`schema_version` (dichiarata DAL PRODUTTORE, deve essere in
`SUPPORTED_SCHEMA_VERSIONS`), `event_id`, `market`, `selection`, `odds`
(**minimo 0.1**, finita), `timestamp` (UTC), `source` (provider), `gateway_id`
(chi l'ha ingerita). Contesto facoltativo: `event_name`, `league`, `home`,
`away`, `kickoff`, `selection_label`, `depth_usdc`. `extra="allow"`: i campi
sconosciuti del feed NON si perdono (restano in `extra_fields`, ispezionabili)
ma non allargano il contratto. Derivati: `quote_id` (identita' della
RILEVAZIONE), `identity_key` (evento+mercato+esito, stabile nel tempo),
`age_seconds()`, `to_signal_fields()` (il ponte verso il Signal: `outcome`
compare solo per 1X2, il contratto non inventa esiti che il Signal non accetta).

**Separazione struttura/strategia**: il contratto valida la FORMA (campi,
tipi, quota >= 0.1, timestamp con fuso, coerenza mercato/selezione, versione
schema); la STRATEGIA (fascia 1.30-1.80, EV, edge, cap) resta nel Risk Engine.
Se il contratto conoscesse le soglie, ogni cambio di strategia sarebbe un
cambio di schema.

**Normalizzazione deterministica** (tabelle esplicite, mai fuzzy):
`KEY_ALIASES` (nomi di chiave dei feed: `match_id`/`eventId` → `event_id`,
`price`/`odd` → `odds`, `provider` → `source`, `version` → `schema_version`;
**il nome canonico vince sempre**), `MARKET_ALIASES` (`h2h`/`Match Odds`
→ `1X2`, `totals`/`Over/Under` → `OU`) e `SELECTION_ALIASES`
(`home`/`casa`/`1` → `1`, `draw`/`tie` → `X`, `u` → `under`). Il confronto e'
case-insensitive e ignora i separatori (`match_odds` = `Match Odds`).
**Validazione incrociata `MARKET_SELECTIONS`**: `1X2`+`over` e `OU`+`2` sono
RIFIUTATI — e' la classe del 09/09 (un `over` saldato su un 1X2).

**Ingresso** (`parse_quote` strict, `validate_batch` non bloccante):
- `parse_quote(row, gateway_id=..., source=..., schema_version=..., assume_utc=...)`
  solleva `MarketQuoteError` con **tutti** i problemi (`issues` machine-readable,
  `QuoteErrorCode`) e **logga ognuno**: una riga `logger.error` + un evento
  JSON `market.quote_rejected` (con `error_code`, campo, gateway, source,
  event_id). Un timestamp senza fuso dice come rimediare
  (`assume_utc=True` se la fonte e' UTC); l'accettazione non emette eventi
  (niente flood: `log_accepted=True` per averli).
- `validate_batch(rows, ...)` = ingresso a LOTTI: accettate + respinte +
  `by_code()`, **mai un'eccezione** (nemmeno con righe ostili), riepilogo
  `market.batch_validated` + riga di log. Anti-flood: gli eventi di rifiuto si
  fermano a `max_events` (20) e il resto finisce in `suppressed_events`.
- `gateway_id`/`source`/`schema_version` passati come default di feed si
  applicano **solo se assenti** nella riga: mai una sovrascrittura silenziosa.
- Nei log NON finisce mai il payload intero: solo nome del campo e valore
  troncato (`TRUNCATE`, 80 char).

**CLI**: `venv/bin/python -m decision market [--file F | --stdin] [--gateway ID]
[--source NOME] [--assume-utc] [--json]` — senza input valida due esempi
integrati (uno conforme, uno respinto su 4 regole). Esce **1** se c'e' almeno
un rifiuto (uso in script), **2** se il JSON non e' leggibile. La CLI usa un
sink nullo: ispeziona i contratti, non scrive sul volume.

**Bug reali trovati scrivendo i test**: 1) una riga ostile (un `Mapping` il cui
`.get` solleva) faceva uscire l'eccezione dall'handler del lotto → la
contabilita' del lotto ora non si fida del dato (`_safe_keys`/`_safe_get`);
2) l'errore di lettura del file/stdin nella CLI era un traceback → ora messaggio
pulito + exit 2.

**Test**: `test_decision_market.py` — **114 verdi, tutti OFFLINE** (dati finti
costruiti a mano, `ListSink` in memoria: **zero crediti API**); pacchetto
`decision` = **350 verdi**. Focus regressioni verde: `test_secret_hygiene`,
`test_auto_bet*`, `test_bot`, `test_risk_guards`, `test_web_api`.
⚠️ Il contratto **non e' ancora usato da nessun feed**: e' il confine pronto per
l'adapter SX/the-odds-api, da collegare quando si sostituira' l'esecuzione.

### Gateway di mercato: SX Bet sorgente PRIMARIA + stop fino alla validazione (15/09/2026)

**Direttiva del proprietario**: SX Bet come sorgente primaria dei dati di
mercato, **refresh forzato del gateway PRIMA del Risk Engine**, blocco del
sistema in caso di fallimento, tracciabilita' di `request_id`, `trace_id`,
`gateway_id`, `schema_version` e `config_hash`, e **stop delle puntate
automatiche mantenuto fino alla validazione** del feed.

**`decision/feeds.py` (nuovo)** — chi porta dentro le quote:
- `SxBetSource` (PRIMARIA): riusa la discovery di `sx_signals`
  (`_discover` + `_books_parallel`: stessa pagina `/markets/active`, stesso
  raggruppamento dei 3 mercati binari, stesso order book taker) con **import
  pigro** — `import decision` resta leggero (tripwire). Provider iniettabile →
  la suite e' OFFLINE. **Zero credenziali, zero crediti, zero ordini**; le
  letture sono pubbliche.
- `MarketFeed` (il gateway): sorgenti ordinate per priorita'
  (`SOURCE_REGISTRY`, primaria da `DECISION_FEED_PRIMARY`, default `sxbet`;
  **nome sconosciuto = nessuna fonte**, mai un ripiego silenzioso), contratto
  validato all'ingresso con `validate_batch` (un'unica porta: nessuna sorgente
  puo' aggirarlo), **refresh forzato** (`refresh(force=True)`; `--force` dalla
  CLI), finestra di riuso `DECISION_FEED_REFRESH_MIN_SEC` (default 600s: il job
gira ogni 60s e l'exchange va rispettato — "forzato" = *non usare una cache
vecchia*, non *martellare*), stato su volume
  (`DATA_DIR/decision/feed_state.json`, scrittura atomica).
- `verify_feed()` (gate, puro): **fail-closed** in quest'ordine —
  `FEED_MISSING` (nessun refresh) → `FEED_UNAVAILABLE` (nessuna sorgente ha
  risposto, o quote non conformi al contratto) → `FEED_STALE` (oltre
  `DECISION_FEED_MAX_AGE_MIN`, default 20) → `FEED_NOT_VALIDATED` (serie
  incompleta) → `ok`. Il blocco e' una regola della stessa catena
  (`SafetyBlock`, stage `market`, **precedenza 4**: dopo kill switch, stop-loss e
  pausa settlement — un problema tecnico non scavalca mai un'autorita' umana).
- **VALIDAZIONE = serie, non timbro**: `DECISION_FEED_MIN_REFRESHES` (3) refresh
  consecutivi ok — "ok" = una sorgente ha risposto E zero quote respinte dal
  contratto — **e** almeno una quota validata in totale (un feed vuoto non prova
  nulla). Un fallimento azzera il contatore; lo stato sopravvive ai redeploy; un
  file di stato CORROTTO vale come **non validato** (qui l'incertezza non deve
  aprire le puntate).

**Catena** (`engine.build_plan`): il gate di mercato sta **dopo le autorita' e
prima del Risk Engine**. Con `MarketFeed` il refresh e' forzato DENTRO il motore;
`emit_many` fa **UN refresh per giro** (non uno per segnale). Il default di
`feed_required` segue l'ambiente (`DECISION_FEED_ENABLED`, ON): in produzione il
gate e' obbligatorio, un `DECISION_FEED_ENABLED=0` esplicito lo disattiva (scelta
loggata, non silenziosa). L'identita' del feed viaggia nel piano
(`CommandPlan.market`) e su OGNI refresh viene emesso `feed.refreshed`/
`feed.failed` con i 5 identificatori (`config_hash` = impronta dei limiti
`RiskLimits`, come nel middleware).

**Percorso d'ORDINE (`auto_bet._market_feed_gate`)**: e' li' che oggi si ordina
davvero, quindi il gate vale anche li' — se il feed non e' valido il giro
**non parte** (nessun ordine, nessuna riga sul ledger; vale anche per SIM, che
alimenta ML/CLV). Fail-closed anche sulle eccezioni impreviste. Notifica admin
una-volta-al-giorno (chiave `FEED_BLOCKED`, inserita dopo i controlli KS e
stop-loss nel job). Cosi' **lo stop resta finche' il feed non e' validato**: non
serve armare a mano, si riapre da solo alla validazione (o prima con la CLI).

**CLI**: `venv/bin/python -m decision feed [--json] [--refresh] [--force]` —
stato, validazione, freschezza e identificatori; `--refresh` esegue UN refresh
reale (lettura pubblica SX). Exit 1 se il gate bloccherebbe le puntate. La CLI
usa un sink nullo: ispeziona, non scrive eventi sul volume.

**Verifica REALE dell'adapter (15/09, 12:37 UTC)**: `--refresh --force` → **81
quote** conformi al contratto, 0 respinte, 27 eventi 1X2 in finestra (Coppa
Italia, La Liga, Superettan...), `inv_sum` riportato come dato (1.0188),
`market_hash` e `sport_x_event_id` nei campi extra; validato al 3° refresh
consecutivo, gate `ok`. Esempio di quota: Genoa–Südtirol `2` @ 6.8966, depth
279 USDC, kickoff UTC.

**2 BUG REALI trovati scrivendo/verificando** (entrambi fixati):
1) **Chiave del book SX**: l'order book espone il lato scommesso con la chiave
   INTERA `1` (la `2` e' il complementare "Not X"), non con la stringa
   dell'esito → il feed risultava **vuoto** col refresh vero (0 quote su 27
   eventi). Ora legge `book.get(1)` come `sx_signals.scan`; se NESSUN book e'
   leggibile la sorgente e' dichiarata **giu'** (`SourceUnavailable`) invece di
   sembrare un mercato vuoto.
2) **`secure_logging` corrompeva gli argomenti con un dict** (BUG DI
   PRODUZIONE, pre-esistente): `logger.warning("... %s", dati)` con un dict fa
   mettere il DICT in `record.args`; il filtro lo iterava come sequenza →
   `TypeError: not all arguments converted during string formatting` alla
   scrittura, cioe' un log che rompe l'handler (emerso da
   `test_bot.py + test_decision_adapters.py` nello stesso processo). Ora i
   mapping sono mascherati MANTENENDO il mapping (test dedicato in
   `test_secure_logging.py`).

**Test**: `test_decision_feed.py` (55, tutti OFFLINE con provider SX finto) +
`test_auto_bet.py::TestGateDiMercato` (6: feed non validato/refresh
fallito/nessuna sorgente → 0 puntate, `market_gate_status()`, errore imprevisto
fail-closed, feed disattivato non blocca). Pacchetto `decision` = **405 verdi**;
focus regressioni verde: `test_auto_bet*`, `test_bot`, `test_secure_logging`,
`test_risk_guards`, `test_settlement_pause`, `test_web_api`.
`verify_guardrails.py` resta offline (`DECISION_FEED_ENABLED=0`: la diagnostica
non tocca la rete) e i 5 guardrail A-E continuano a bloccare.

**Env dichiarate** in `.railway/railway.ts` (blocco `decisionEnv`, solo servizio
`api`: il cron surebet non usa il pacchetto): `DECISION_FEED_ENABLED`,
`DECISION_FEED_PRIMARY`, `DECISION_FEED_MAX_AGE_MIN`,
`DECISION_FEED_MIN_REFRESHES`, `DECISION_FEED_REFRESH_MIN_SEC`,
`DECISION_FEED_STATE`, `DECISION_SHADOW`, `DECISION_LOG_SINK`,
`DECISION_LOG_MAX_MB`, `DECISION_SHADOW_LOG`, `DECISION_OBSERVABILITY`,
`DECISION_MIN_MODEL_COVERAGE`, `DECISION_REVIEW_*`. `railway config plan` dopo
la modifica: **0 to add, 1 to change, 0 to destroy** (l'unico cambio e' il flag
non distruttivo di api-volume). ⚠️ Le env NON sono ancora su Railway: valgono i
default di codice (feed ON, primaria sxbet, 3 refresh, 20 min, 600s).

**⚠️ STATO OPERATIVO (verificato sul container il 15/09)**: `auto_bet_mode.json`
= **`{"mode": "live"}`** (armato dal 12/09) e `settlement_paused.json` =
`{"paused": false}`, `STAKE_CAP_HARD=1`, `DECISION_FEED_ENABLED` assente →
feed ON di default. Con il codice attuale **il gate di mercato terra' ferme le
puntate finche' il feed non e' validato** (3 refresh conformi consecutivi): la
validazione si accumula da sola quando il percorso ordini gira, oppure a
richiesta con `python -m decision feed --refresh --force` (lettura pubblica SX,
**zero crediti** e nessun ordine) — utile per non aspettare i giri del job.
Il kill switch resta un'autorita' superiore: `/autobet off` ferma tutto a
prescindere dal feed.

**⚠️ Stato**: in produzione la catena resta **shadow** (nessun ordine cambia).
Restano da fare, in ordine: (a) ✅ coda revisioni su Telegram con bottoni
approva/rifiuta e callback idempotenti (15/09, `decision/review_telegram.py`:
vedi l'ultima sezione); (b) leggere il registro
shadow dopo qualche giorno e confrontarlo con le puntate reali; (c) ✅ adapter
di feed reale (`decision/feeds.py`, SX primaria) collegato al gate della catena
E al percorso d'ordine; (d) **lasciar girare il feed fino alla validazione**
(3 refresh conformi consecutivi) e poi leggere il registro shadow; (e) solo
dopo, sostituire l'esecuzione di `auto_bet` col percorso Command (passo 3).
Env nuove da dichiarare in `preserve()` di `.railway/railway.ts` prima di un
`config apply`: `DECISION_SHADOW`, `DECISION_LOG_SINK`, `DECISION_LOG_MAX_MB`,
`DECISION_SHADOW_LOG`, `DECISION_OBSERVABILITY`, `DECISION_MIN_MODEL_COVERAGE`,
`DECISION_REVIEW_ENABLED`, `DECISION_REVIEW_QUEUE`, `DECISION_REVIEW_CONFIDENCE`.


### Revisioni umane su Telegram: callback idempotenti (15/09/2026)

Chiude il punto (a) dei prossimi passi della catena `decision/`: la coda delle
revisioni (`reviews.json`) non era piu' un file che nessuno guardava — ora il
verdetto `review` diventa un **messaggio con due bottoni** e il click e'
idempotente.

**Nuovo modulo `decision/review_telegram.py`** (nessun import di produzione a
livello di modulo: `import decision` resta leggero, tripwire in
`test_decision_review_telegram.py`):

| pezzo | cosa fa |
|---|---|
| `callback_id(record_id, action)` | chiave `rv:<a\|r>:<token10>` **stabile** (funzione pura): lo stesso bottone ha sempre la stessa chiave, prima e dopo un redeploy. Il `record_id` NON viaggia nel callback (e' un digest): un payload manomesso non puo' puntare a una revisione arbitraria. |
| `parse_callback(data)` | STRETTO: prefisso + azione + token validati; tutto il resto e' `CallbackError`. `is_ours()` distingue "non nostro" (silenzio) da "nostro malformato" (errore loggato). |
| `CallbackStore` | store sul volume (`DATA_DIR/decision/review_callbacks.json`, env `DECISION_CALLBACK_STORE`), scrittura atomica: `resolved` (idempotenza) + `prompts` (anti-spam). Fail-safe: file corrotto = store vuoto e **non** sovrascritto; scrittura impossibile = `False`, mai un'eccezione. |
| `build_prompt(entry)` | testo + tastiera inline (✅ Approva / ❌ Rifiuta). Pura: si ispeziona in un test senza Telegram. Mostra quota, mercato, blend, edge, EV, confidenza, **copertura ratings**, motivo, kickoff e modalita'. |
| `send_prompts(...)` | invia i prompt agli admin (`ADMIN_CHAT_ID`), marca il prompt sullo store (idempotente): il job gira spesso e non ripete. Invio fallito → marker NON scritto (si ritenta). |
| `handle_callback(data, ...)` | chiude la revisione: `pipeline.resolve_review` + `engine.plan_for_resolved` + `Dispatcher`. Ritorna SEMPRE un `ReviewOutcome` (mai un'eccezione). |
| `answer_callback(query, ...)` | risponde alla callback query E modifica il messaggio (esito + bottoni rimossi). La risposta viene inviata anche sui duplicati: e' cio' che ferma i redelivery di Telegram. |

**Idempotenza a tre livelli** (due click, un redelivery, un riavvio a meta'
lavoro = UNA decisione, UN dispatch):
1. la chiave e' stabile per costruzione;
2. lo store ritrova la chiave risolta e restituisce l'esito invariato (nessuna
   risoluzione, nessun dispatch);
3. la coda e' idempotente per conto suo (`_decide` su una voce gia' decisa).

In piu' il **claim prima, completamento dopo**: se il processo muore fra la
decisione e il dispatch, al retry si trova un claim senza esito e NON si
ri-dispatcha (meglio una revisione a meta', visibile, che due ordini). Un
callback in `error` si sblocca SOLO a mano (`store.release`): l'idempotenza non
si rompe da sola.

**Nessuna esecuzione**: il set di gateway di default e' lo `ShadowGateway`.
Il click attraversa tutta la catena (decisione, stake, comando d'ordine) ma
*registra*: l'ordine che sarebbe partito finisce nel registro shadow, non
sull'exchange. Per eseguire davvero servono gateway espliciti — oggi nessun
chiamante di produzione li passa (e c'e' un test che lo verifica).

**La coda non si allaga**: `ReviewQueue.add` ora deduplica anche per
`signal_id`, non solo per `record_id` (che porta i secondi e quindi cambia a
ogni giro del job): **una revisione per opportunita'**, e una voce gia' decisa
(approvata o rifiutata) non torna a chiedere. Senza questo, il job ogni 60s
avrebbe prodotto centinaia di copie dello stesso segnale.

**Motore**: nuova `engine.plan_for_resolved(record)` — la mappa
record → comandi scritta UNA volta (persist sempre; `place_order` solo se
`approve` + eseguibile + `mode == live`). `build_plan` ora la usa per il
percorso approve, cosi' il callback non puo' divergere dal motore.

**bot.py**: `CallbackQueryHandler(review_callback_handler, pattern=r"^rv:")`
(solo i nostri callback; solo admin), job `decision_review_job` ogni 5 min
(`run_repeating`, `max_instances=1`) che invia i prompt, comando admin
**`/revisioni`** (stato + chiavi per la CLI). `auto_bet._shadow_run` non cambia
niente di operativo: la coda viene riempita dalla catena shadow (disattivabile
con `DECISION_REVIEWS=0`), quindi l'approvazione di un verdetto shadow resta
shadow.

**CLI**: `venv/bin/python -m decision review [--json] [--all] [--limit N]`
mostra i prompt in attesa con le chiavi; `--callback rv:a:<token>
[--reviewer N --bankroll N --mode live]` SIMULA il click percorrendo la catena
completa coi gateway shadow (idempotenza e mappa dei comandi verificabili senza
Telegram); `--send` invia davvero (rete, scelta esplicita).

**Verifica**: `test_decision_review_telegram.py` (**59 verdi, tutti OFFLINE**:
client Telegram finto, store su tmp, nessuna rete/credenziale/ordine) +
regressioni. Pacchetto `decision` = **462 verdi**. Smoke isolato end-to-end:
segnale → `review` in coda → prompt con chiave `rv:a:...` → click →
`stake 1.00`, comandi `persist_decision` + `place_order` **registrati** nel
registro shadow; **secondo click → duplicato, un solo `place_order` nel
registro**.

**Bug trovati dai test (prima del deploy)**:
1. `CallbackStore.load()` restituiva `{}` senza `resolved`/`prompts` → il primo
   giro su un volume nuovo (o con file corrotto) andava in `KeyError`: ora la
   struttura e' sempre ben formata (`_empty()`);
2. **due test trappola sulla data** (non miei, pre-esistenti): `test_sx_signals`
   (bet aperta con kickoff fisso 09/09 che, superati i 5 giorni di
   `SX_STALE_DAYS`, veniva scaduta come push) e `test_settlement_sanity`
   (partita "corrente" 31/08 fuori dalla finestra cassa di 14 giorni). Ora usano
   date RELATIVE a `now`: un test che scadeva col calendario e' un falso allarme
   che arriva sempre nel momento peggiore.

**Env dichiarate** in `.railway/railway.ts` (`decisionEnv`, solo `api`):
`DECISION_REVIEWS` (interruttore della coda) e `DECISION_CALLBACK_STORE`.
`railway config plan`: **0 to add, 1 to change, 0 to destroy**.

**⚠️ Resta shadow**: in produzione nessun ordine cambia. Il percorso ordini di
`auto_bet` continua a girare e il gate di mercato (feed SX, 3 refresh conformi)
resta l'autorita' sull'esecuzione. Prossimo passo: leggere il registro shadow
dopo qualche giorno e decidere il passo 3 (sostituire l'esecuzione col percorso
Command), con i bottoni Telegram gia' pronti a governare le revisioni.

### Fix stop-loss sull'EQUITY + monitor del ritmo crediti (15/09/2026, sera)

**1) BUG STOP-LOSS: scattava sull'ESCROW, non sulle perdite (fixato).**
`auto_bet` leggeva `availableBalance` e lo usava come bankroll, poi
`check_daily_stop()` misurava la perdita su quello. Ma `availableBalance`
**esclude i fondi in escrow delle bet aperte**: piazzare una bet abbassa il
disponibile e sembra una perdita. Prova sul volume: `start_bankroll 35.9779`,
disponibile `33.9779`, exposure `2.0` → il -5.6% era **esattamente l'escrow**
(equity reale 35.98, zero denaro perso). Timing: bet piazzate alle
`18:59:15`/`18:59:16`, stop armato alle **`18:59:25`** — 9 secondi dopo, per
24h: con ~34 USDC nel wallet bastavano **2 bet aperte** per bloccare il bot.
- `_live_wallet_balance()` → **`_live_wallet_snapshot()`**: ritorna
  `{"available", "exposure", "equity"}` (None se non leggibile/dry-run).
  `equity = available + exposure` (l'esposizione arriva da
  `get_balance()["exposure"]` = escrow + pending).
- In LIVE il **bankroll del Kelly, la drawdown protection e lo stop-loss**
  usano l'EQUITY; il **DISPONIBILE** resta il vincolo di cassa del singolo
  ordine (`_spendable`): l'equity non si spende due volte.
- `check_daily_stop(bankroll, basis=...)`: il motivo/log dichiara su quale
  valore e' misurata la perdita ("equity wallet" in LIVE, "cassa" in SIM).
- Se il provider non espone `exposure` la stima e' PRUDENTE (equity = solo
  disponibile): lo stop puo' scattare prima, mai dopo (fail-closed).
- `/autobet` mostra ora `equity (liberi + in gioco)` e il cap severo si
  misura sull'equity (prima l'operatore leggeva un bankroll diverso da quello
  usato dallo staking).
- **`verify_guardrails.py` scenario C** aggiornato (36 liberi + 2 in gioco =
  38 equity): i 5 guardrail A-E continuano a bloccare.
- Tripwire: `TestBankrollEquity` in `test_auto_bet_live.py` (bet piazzata NON
  arma lo stop — regressione del bug; perdita vera sull'equity arma ancora;
  lo stake non supera i fondi liberi; snapshot da provider finto, senza
  `exposure`, dry-run/errore → None) + `test_basis_dichiarato_nel_messaggio`
  in `test_risk_guards.py`.

**2) MONITOR DEL RITMO CREDITI (`odds_api.credit_burn_rate` +
`credit_budget_status`).** Le soglie fisse (50/20/10/5) tacciono sopra 50 e
avvisano quando il budget e' gia' compromesso; la media lunga mente (il 15/09
la finestra a 340h diceva **15.5 crediti/giorno**, le ultime 24h ne dicevano
**57.8**: la media includeva la pausa del settlement e il cambio di chiave).
- `credit_burn_rate(window_hours=48)`: consumo MISURATO fra la lettura piu'
  vecchia e la piu' recente della finestra (`remaining_ts`), con fallback
  dichiarato sulla storia disponibile; None se i crediti risalgono (chiave
  cambiata) o non c'e' niente da misurare.
- `credit_budget_status()`: residuo, ritmo, `days_left`, `exhaustion_date`,
  `sustainable_per_day` e **`alert`** = il ritmo esaurisce i crediti PRIMA del
  reset. `CREDITS_RESET` (01/10) e `days_to_reset()` ora vivono QUI (prima la
  data era duplicata in `web_api`).
- **`credit_watchdog_job`** (ogni 6h): logga SEMPRE il ritmo e allerta
  admin+iscritti con anti-spam 1/giorno (chiave `CREDIT_BURN`) quando il
  budget non arriva al reset, elencando i costi da tagliare.
- **`GET /api/credits`**: `remaining` = lettura AUTOREVOLE (l'ultima, la
  stessa di `get_remaining`), `remaining_min` = diagnostica; `status` e
  `sustainable_daily` seguono il valore vero (prima il MINIMO fra le cache
  inchiodava il numero: 12/09 reale 452, mostrato 58). `estimated_daily_consumption`
  e' il ritmo MISURATO (`consumption_source` = measured|heuristic),
  `days_left_at_current_rate` la proiezione. Fix collaterale: la chiave sport
  dei nomi cache lasciava il `.json` attaccato e non trovava il titolo lega
  (`toa_scores_soccer_italy_serie_a.json` → 'Serie A').
- Test: 7 nuovi in `test_odds_api.py` (ritmo recente, finestra vera nel
  fallback, None se risalgono, nessun alert inventato, alert/non-alert con
  `now` FISSO per non scadere col calendario) + `TestCredits` in
  `test_web_api.py` (lettura autorevole, stato coerente, consumo misurato,
  titolo lega, nessuna telemetria).

**3) MISURA REALE SUL CONTAINER (15/09, sera) — dove vanno i crediti.**
273 crediti residui, **27 chiamate `scores` in 24h (56 crediti = ~2.07 a
chiamata), ZERO chiamate di rotazione quote**: il consumo e' **tutto
settlement**, ~15-21 leghe con righe aperte riscaricate a OGNI giro del
watchdog (ogni 4h, "le leghe con righe aperte si interrogano sempre"). A
57.8/giorno i 273 crediti finiscono il **~20/09**, prima del reset (01/10,
18.2/giorno sostenibili).
⚠️ **`should_query_sport()` NON copre il settlement** (e' usato solo in
`_get_odds`): sotto la soglia 50 la rotazione si riduce ma il costo dominante
no. Leva proposta (decisione del proprietario, non applicata): dare alle
leghe con righe aperte un intervallo minimo di refetch (es. 6-12h) o saltare
le heur-only sotto soglia — il risultato di una partita non cambia fra un
giro e l'altro, quindi la chiusura ritarda di ore senza costi aggiuntivi.

**4) DEPLOY del lavoro del 15/09.** La catena `decision/` (command pattern,
fail-fast, observability, shadow mode, contratto di mercato, feed SX, coda
revisioni Telegram) era **installata ma inerte**: `origin/main` era fermo a
`90a988a` (14/09) e 36 file erano non committati. Prima del push: 0 marker di
conflitto, **1688 test offline verdi** (4 lotti, `-m "not integration"`),
`compileall` OK. Dopo il deploy in produzione nascono `data/decision/` (eventi,
registro shadow, coda revisioni) e la tabella `decisions`; il **gate di
mercato** (feed SX, 3 refresh conformi) diventa l'autorita' sull'esecuzione e
le revisioni si governano dai bottoni Telegram.

**5) BUG trovato IN PRODUZIONE al primo giro dopo il deploy (fixato).** Il
log del giro 17:36 UTC diceva `adapter: 3 segnali aperti su 4 righe di ledger`
e la riga scartata era **`Atlético Madrid vs Osasuna`** con
`esito: 'Atlético Madrid'`. Causa: il ledger `predictions` **non e' omogeneo**
— `sx_signals` scrive `1`/`X`/`2`, `fixture_engine` scrive il **nome della
squadra giocata** — e `signal_from_row` pretendeva `outcome in ("1","X","2")`,
quindi scartava in silenzio proprio le righe della produzione (la catena
misurava un insieme DIVERSO da quello su cui `auto_bet` scommette: il shadow
perdeva il suo senso). Fix: nuova **`decision.adapters.canonical_outcome`**
(stessa semantica di `auto_bet._canonical_esito`) — pass-through di 1/X/2,
alias di pareggio, nome grezzo/RISOLTO (`team_names.resolve_team_name`,
iniettabile) e `team_names.same_team` per codici di stato/sigle; se il nome
coincide con ENTRAMBE le squadre o con nessuna → `None` (mai indovinare).
`signal_from_row` accetta ora `resolve=` e `iter_signals` glielo passa.
Test: `TestEsitoCanonico` in `test_decision_adapters.py` (forme canoniche,
nome squadra → 1/2, tolleranza su codici di stato, mai indovinare, riga
`Inter` → Signal con `prob_1`, regressione sul ledger misto a 4 righe).

**Primo ordine reale dopo il fix (15/09 17:36 UTC)**: bet #41 `sx-L20021478`
(Liverpool vs Tottenham, esito 1) **1.00 USDC @ 1.8561**, FULLY_FILLED
(il floor EV era 1.86 → prezzo migliore). Il candidato Atlético Madrid è
stato saltato per prezzo (`best sxbet 1.42 < floor 1.54`), esattamente il
comportamento documentato. **Shadow**: 3 segnali valutati, verdetto
`reject` x3, 3 comandi `persist_decision`, **0 ordini** dalla catena, 0
revisioni in coda (la catena nuova e' piu' severa della corsia di cassa,
com'era previsto: «shadow, nessun ordine cambia»). Wallet 33.98 liberi +
2.00 in gioco → equity 35.98. `STAKE_CAP_HARD=0` (scelta del proprietario
del 12/09): il floor 1 USDC prevale sui cap percentuali, quindi le
puntate riprendono a 1 USDC ciascuna.

### Taglio copertura settlement: il referto segue il DENARO (15/09/2026, sera)

**Direttiva del proprietario**: "riduco la copertura settlement" (l'altra
risposta, sul gate leghe della corsia auto-bet, e' stata "prima misuro").

**Misura che ha motivato il taglio** (sul container, 15/09): il settlement
costava **27 chiamate `/scores` in 24h** (~51-58 crediti/giorno a ~2 crediti
per chiamata) contro **18,1/giorno sostenibili** fino al reset del 01/10 —
esaurimento previsto ~20/09. La voce DOMINANTE erano le leghe con le **sole
previsioni aperte** (telemetria di calibrazione, nessun soldo in gioco),
riscaricate a ogni giro del watchdog (ogni 4h).

**Implementazione** (`tracker.get_leagues_with_open_rows`, `bot._update_results`):
- **`SETTLEMENT_BETS_ONLY` (default ON)**: una lega entra nel piano solo se ha
  una **PUNTATA** nel ledger (reale o simulata) aperta o chiusa da poco. Le
  previsioni delle leghe senza puntate restano aperte fino alla scadenza
  automatica (`expire_stale_sx_rows`, chiusura come push): **si perde
  telemetria di calibrazione, MAI il referto di una puntata**. Ripristino del
  comportamento esteso con `SETTLEMENT_BETS_ONLY=0`.
- **Crediti sotto `CREDIT_LOW` (50)**: la **verifica periodica** (leghe senza
  righe aperte, costo puro che non salda nulla, `SETTLEMENT_HEAL_INTERVAL_HOURS`
  36h) viene **saltata del tutto**. Una puntata aperta si referta comunque.
  Lettura crediti fallita/illeggibile -> comportamento invariato (nessuna
  verifica saltata in silenzio).
- **Politica dichiarata nei log e nel residuo**: nuova
  `tracker.settlement_coverage_policy()` ("solo-puntate,
  verifica-periodica-saltata (crediti scarsi)") stampata nella riga
  `settlement: N leghe interrogate ... politica ...`; `settlement_residue()`
  espone `bets_only` e `heal_skipped_low_credits`. Senza dichiararla, un
  residuo piu' basso sembrerebbe un referto migliore invece di una scelta.
- **Test**: `test_settlement_watchdog.TestCoperturaSettlementSoloPuntate`
  (5 test: lega senza puntate non interrogata + controprova estesa, env che
  riattiva la copertura, verifica periodica saltata sotto soglia crediti con
  la puntata che resta nel piano, crediti illeggibili che non cambiano il
  piano, politica dichiarata, tripwire sul pianificatore in `bot.py`).
  `TestResiduoSettlement` ora gira esplicitamente con `SETTLEMENT_BETS_ONLY=0`
  (la sua classificazione dei motivi e' quella a copertura estesa).

### Gate leghe sulla corsia auto-bet: MISURATO (15/09/2026, sera)

**Direttiva del proprietario**: "prima misuro" (prima di allineare la corsia
auto-bet a `STRATEGY_LEAGUES`). Misura fatta, in sola lettura sul ledger di
produzione, con il nuovo `league_gate_impact.py` (zero ordini, zero crediti,
connessione SQLite `mode=ro`).

**Procedura attuale (bug di propagazione, non scelta)**: i candidati di
`fixture_engine` e `sx_signals` NON portano la chiave `league`, quindi
`is_sane(league="")` tratta la lega vuota come AMMESSA: la strategia "solo
campionati vincenti" (5 leghe ammesse) e' applicata dalla catena `decision/`
(che infatti rifiuta con `league_not_allowed`) ma **non dalla corsia che
piazza davvero**.

**A. Il gate NON e' giudicabile sul P/L con questo ledger** (tutto lo storico,
`--source all`; le due fonti NON si sommano — il tool le separa):

| gruppo | righe | segnali giocabili (chiusi) | PUNTATE | PREVISIONI (per unita') |
|---|---|---|---|---|
| ammesse | 18 | 7 (7) | n=1, P/L +0.99, ROI +99% | 15 chiuse, ROI **+29.8%** |
| bloccate | 78 | 11 (4) | n=1, P/L -1.00, ROI -100% | 31 chiuse, ROI **-22.8%** |
| senza lega | 278 | 107 (107) | 36 chiuse, ROI -11.2% | 205 chiuse, ROI -13.2% |

**7 segnali giocabili chiusi nelle ammesse contro 4 nelle bloccate**: sotto la
soglia di affidabilita' (30) che il tool dichiara, quindi il P/L non decide
nulla. Il dato che invece conta: **278 righe su 374 (74%) non hanno lega** (le
36/41 puntate storiche senza riga in `matches`) — finche' la lega non viene
propagata il ledger non potra' decidere il gate.

**B. Il FLUSSO invece e' decisivo: il gate spegnerebbe la corsia.**
Ultimi 3 giorni (finestra mirata), 91 righe analizzate su **33 leghe**, di cui
**13 giocabili**:
- leghe ammesse: **17 righe (19%) con 6 giocabili**;
- leghe bloccate: 74 righe con **7 giocabili (il resto sono righe rejected)**.
Nel campione piu' stretto (83 righe, i soli segnali prodotti dai due cantieri
nelle ultime 72h) le ammesse davano **11 righe (13%) e 0 giocabili**, mentre le
5 giocabili erano **tutte in leghe vietate**. Le 3 bet live aperte del 15/09
sono tutte in leghe vietate (Scottish Premiership, EFL Championship, EFL Cup) e
il registro shadow ha respinto **4/4** i segnali aperti con
`league_not_allowed`.
→ Applicare oggi il gate alla corsia auto-bet = **molto vicino a zero
puntate** (0 sui segnali giocabili delle ultime 72h). La decisione resta del
proprietario: il trade-off e' "prudenza" contro "copertura dei campionati dove
la strategia e' stata validata" — e per misurare il secondo serve prima la lega
sui candidati (`fixture_engine`/`sx_signals` non la passano).

**Nuovo strumento `league_gate_impact.py`** (diagnostica, NON decisionale):
- `measure(days, source)` → bucket ammesse/bloccate/**senza lega** con righe,
  in gioco, chiuse, **segnali giocabili (e chiusi)**, dettaglio per lega
  bloccata, righe in gioco che il gate bloccherebbe subito, sezione `coverage`
  (flusso) e `reliable` + `caveat`.
- **Le due fonti non si sommano** (difetto trovato MISURANDO in produzione il
  15/09): `bets.profit` e' valuta, `predictions.profit` e' per unita' di stake
  -> con `--source all` l'aggregato si azzera (`mixed: true`) e i numeri buoni
  restano in `by_source`, uno per fonte. L'affidabilita' si conta sui **segnali
  giocabili chiusi**, non su tutte le righe: le previsioni `rejected` dicono
  cosa il gate taglierebbe ma non sono giocate.
- **Assi temporali diversi e voluti**: i bucket P/L usano la **data del match**
  (kickoff, fallback data di registrazione), la `coverage` usa la **produzione
  del segnale** (`predictions.created_at`): le previsioni nascono 1-3 giorni
  prima del kickoff.
- CLI: `venv/bin/python league_gate_impact.py [--days N] [--source bets|all] [--json]`.
- Garanzie verificate dai test: nessuna scrittura (mode=ro, nessun
  INSERT/UPDATE/DELETE nel sorgente), nessuna rete (nessun `odds_api`/
  `fetch_scores`/`sx_signals`), errori mai propagati.
- Test: `test_league_gate_impact.py` (21 verdi, ledger temporaneo).

### Shadow Validation: stato `pending` + convalida della riga persistita (16/09/2026)

**Richiesta del proprietario**: "integra la logica di Shadow Validation subito
dopo la funzione di salvataggio nel gateway di storage; ogni valutazione
persista nel database prima di essere elaborata nell'engine di convalida,
mantenendo lo stato su 'pending' e bloccando l'invio reale fino all'esito
positivo; includi record ID e trace ID nei log del middleware".

**⚠️ Nota di partenza: "Shadow Validation" NON esisteva in questo progetto.**
Prima di scrivere codice la richiesta e' stata mappata sui componenti reali e
le due scelte non ovvie sono state chieste al proprietario:

| termine della richiesta | cosa e' stato deciso |
|---|---|
| "gateway di storage" + "funzione di salvataggio" | `LedgerGateway._run` → `feedback.persist(row)` |
| "engine di convalida" | **nuovo** `decision/validation.py` (non esisteva) |
| stato "pending" | **nuovo** stato sul ledger `decisions` (la tabella non aveva `status`) |
| "bloccare l'invio reale fino all'esito positivo" | nuovo flag **opt-in** `Dispatcher(require_persist=True)` |
| "Shadow Validation" | vivere **dentro** il gateway di storage + opt-in nella shadow mode |

Decisioni prese: **(1)** stato `pending` sul ledger **+** persistenza delle
valutazioni shadow (opt-in, deduplicate); **(2)** `require_persist` come flag
**opzionale, default invariato** (fail-soft storico).

**1) IL CICLO DI VITA DIVENTA A TRE STATI.** `"persistito" non e' "approvato"`:
la riga **nasce `pending`** col salvataggio, il motore di convalida la
**rilegge dal ledger** e solo `validated` autorizza l'ordine.

- `tracker.py`: colonna `status` in `DECISION_FIELDS` (migrazione ALTER
  idempotente, come le altre), indice `idx_decisions_status`, costanti
  `DECISION_STATUS_PENDING/VALIDATED/REJECTED`, piu' `get_decision(record_id)`,
  `set_decision_status(record_id, status)`, `decision_exists_for_signal(signal_id)`,
  filtro `get_decisions(status=...)` e `decision_stats()["by_status"]`.
  Le righe legacy (status NULL) **non** vengono contate come `pending`: non si
  inventa uno stato che non c'e'.
- `decision/models.py`: `DecisionStatus` + `DecisionRecord.status` (`pending` di
  default) + i nuovi `ReasonCode` `STAKE_NOT_EXECUTABLE` e `VALIDATION_INCOMPLETE`.
  Il motore di decisione **non tocca** lo stato: lo muove solo la convalida, cosi'
  "decidere" e "convalidare" restano due atti distinti.
- Gli stati sono **duplicati di proposito** in `tracker` e `decision.models` (il
  ledger non importa il pacchetto di decisione e viceversa, tripwire incluso) e
  un test confronta le due tabelle di stringhe: non possono divergere in silenzio.

**2) `ValidatingLedgerGateway`: la convalida sta DENTRO il gateway di storage.**
Ordine esatto dei passi, garantito dal test (`persist → read → write`):

    1. SALVA      la riga sul ledger (stato `pending`)
    2. RILEGGE    cio' che e' stato scritto  ← NON l'oggetto in memoria
    3. CONVALIDA  la riga (`decision/validation.py`, motore PURO)
    4. SCRIVE     lo stato risultante (`validated` / `rejected` / `pending`)

*Perche' la convalida legge la riga e non l'oggetto*: esaminando l'oggetto, una
scrittura fallita non si vedrebbe e la catena autorizzerebbe un ordine in nome
di una decisione che sul ledger non esiste. Leggendo cio' che e' stato scritto,
"prima persistere, poi convalidare" e' una proprieta' **strutturale**.
*Perche' nello stesso gateway e non in un comando separato*: riga e stato sono
due meta' dello stesso atto di audit — tenerli insieme rende impossibile
convalidare qualcosa che non e' stato scritto, senza aggiungere un tipo di
comando che ogni fabbrica del motore dovrebbe emettere in coppia (e ricordarsi
di non dimenticare). **Nessun comando nuovo, `COMMAND_ORDER` invariato,
`by_command` della shadow mode invariato**: i 22 test preesistenti della shadow
non sono stati toccati.

**3) REGOLE DI CONVALIDA (nessuna soglia copiata, nessun fuzzy).** Il verdetto
e' gia' scritto nella riga: qui si traduce una riga in uno stato.

| riga persistita | stato | motivo |
|---|---|---|
| verdict `reject` (kill switch, feed, gate) | rejected | il motivo del rifiuto (letto, mai inventato) |
| verdict `review` | **pending** | `review_pending` (un umano puo' ancora promuoverla) |
| verdict `approve` + stake eseguibile | validated | `ok` |
| verdict `approve` senza stake eseguibile | rejected | `stake_not_executable` (cap severo/floor) |
| verdict assente/ignoto | **pending** | `validation_incomplete` |
| riga assente o senza `record_id` | **pending** | `validation_incomplete` |

`pending` blocca l'ordine esattamente come `rejected`: la differenza e' che
`pending` puo' ancora diventare `validated`, `rejected` e' definitivo.

**4) IL BLOCCO DELL'ORDINE (`Dispatcher(require_persist=True)`, opt-in).**
Un `place_order` viene **saltato** se il `persist_decision` del piano e'
fallito, se **non c'e' affatto** un `persist_decision`, o se la convalida ha
dato esito non positivo (`data["validated"] != True`). Con
`require_persist=False` (default) il dispatcher resta **fail-soft** come prima.

- **Blocca SOLO l'ordine**: audit e notifiche proseguono. Una revisione umana
  deve poter arrivare anche se il ledger ha avuto un problema — un guasto di
  telemetria non deve diventare un silenzio operativo (test dedicato).
- Un gateway di storage **senza** convalida (il vecchio `LedgerGateway`) non ha
  `data["validated"]`: vale il salvataggio riuscito (retrocompatibilita'
  esplicita, verificata dai test).
- `DispatchReport` ha ora `aborted` e `blocked_reason`, e l'evento
  `order.blocked` registra il motivo.
- I gateway di **solo audit** (`audit_only = True` su `LedgerGateway` e
  `ValidatingLedgerGateway`) sono esclusi da `DispatchReport.shadow`: senza
  questa distinzione un giro in shadow mode con la persistenza attiva si
  dichiarerebbe "non shadow" pur non avendo eseguito nulla sul mondo.

**5) TRACCIABILITA' NEI LOG DEL MIDDLEWARE.** `record_id` e `signal_id` sono
ora su `plan.dispatch`, sugli span `command.*`, su `command.result` (col campo
`validated` della convalida) e su `plan.dispatched`; il `trace_id` (e
`request_id`/`span_id`/`parent_span_id`) c'era gia' perche' arriva dal
`TraceContext`. Test: `TestTracciabilita` (5 test, con trace fissa).

**6) SHADOW MODE: persistenza OPT-IN (`DECISION_SHADOW_PERSIST`, default OFF).**
Con l'interruttore attivo `run_shadow` registra **prima** il
`ValidatingLedgerGateway` e poi lo `ShadowGateway` (il dispatcher sceglie il
primo che sa gestire il comando): il `persist_decision` scrive **davvero** sul
ledger, mentre `place_order`/`notify_operators` restano al registro shadow.
`require_persist=True` garantisce che l'ordine risulti "sarebbe partito" solo a
convalida positiva. `out` riporta `persist_enabled`, `persisted`,
`persisted_duplicates`, `order_blocked`.

**⚠️ DEVIAZIONE DICHIARATA dalla lettera della richiesta: la deduplicazione e'
per `signal_id`, NON per `record_id`.** Il `record_id` ha granularita' al
**secondo** e cambia a ogni giro del job (60s), quindi deduplicare su di esso
non deduplicherebbe nulla: la stessa opportunita' finirebbe sul ledger fino a
1440 volte al giorno. `signal_id` (match+mercato+esito) e' invece stabile.
*Limite noto e accettato*: un segnale viene registrato alla **PRIMA**
valutazione — se il prezzo si muove dopo, la riga non si aggiorna (una riga per
opportunita', non un diario di ogni giro). Il contatore `persisted_duplicates`
rende il fenomeno visibile.

**7) IMPATTO IN PRODUZIONE: ZERO, per costruzione.** `DECISION_SHADOW_PERSIST`
default OFF → la shadow mode non scrive sul ledger come prima;
`require_persist` default False → il percorso d'ordine di `auto_bet` non cambia.
Il nuovo stato `pending` **esiste** ma nessuno lo usa finche' (a) non si accende
l'interruttore o (b) non si passa al percorso Command (passo 3). I test
preesistenti della shadow che asserivano "nessuna riga sul ledger" passano
invariati e lo dimostrano.

**Test**: `test_decision_validation.py` **66 verdi, tutti OFFLINE** (gateway
finti in memoria, SQLite temporaneo, zero rete/credenziali/ordini); pacchetto
`decision` = **539 verdi**. Regressioni verdi: `test_auto_bet*` (3 file),
`test_favourites_only`, `test_risk_guards`, `test_bot`, `test_web_api`,
`test_reports`, `test_settlement_watchdog`, `test_sx_native_settlement`,
`test_secret_hygiene`, `test_performance_report`. `verify_guardrails.py`:
**A–F tutti bloccano** (invariato). `compileall` OK.

**IaC**: `DECISION_SHADOW_PERSIST` dichiarata `preserve()` nel blocco
`decisionEnv` di `.railway/railway.ts` (solo servizio `api`).
`railway config plan` dopo la modifica: **0 to add, 1 to change, 0 to destroy**
(l'unico cambio e' il flag non distruttivo `api-volume config.isCreated`).

**Prossimo passo naturale**: accendere `DECISION_SHADOW_PERSIST=1` su Railway per
far girare la convalida sui segnali veri (una riga per segnale, zero ordini) e
leggere `decision_stats()["by_status"]` + il registro shadow dopo qualche
giorno — e' il dato che serve prima di decidere il passo 3 (sostituire
l'esecuzione di `auto_bet` col percorso Command).

### Fase di confronto shadow: catena ↔ corsia (`decision/compare.py`, 16/09/2026)

Aperta su indicazione del proprietario subito dopo la Shadow Validation: le due
strade vengono messe a confronto su dati reali, in **sola lettura**, perche' il
passo 3 (sostituire l'esecuzione di `auto_bet` col percorso Command) si decida
su numeri e non su impressioni.

**Le due strade, un registro ciascuna.**

| strada | ledger | cosa contiene |
|---|---|---|
| catena (`decision/`) | `decisions` | verdetto + `ReasonCode` + stato di convalida (per ogni segnale VALUTATO) |
| corsia (`auto_bet`) | `bets` | una riga **solo se la puntata e' stata piazzata** (sim o live) |

Chiave di giunzione: **(match_id, esito canonico)**. L'esito della corsia e'
normalizzato con `decision.adapters.canonical_outcome` (import pigro): lo stesso
ledger misto che il 15/09 aveva ingannato l'adapter (nomi squadra nelle righe di
`fixture_engine`, `1`/`X`/`2` in quelle di `sx_signals`) non falsa il confronto.

**I cinque casi (esaustivi: nessuna riga sparisce in silenzio).**

| caso | catena | corsia |
|---|---|---|
| `both_play` | approva + stake eseguibile | ha puntato |
| `blocked_played` | rifiuta / non eseguibile | **ha puntato** |
| `would_play_skipped` | approva + stake eseguibile | **non ha puntato** |
| `agree_skip` | rifiuta | non ha puntato |
| `unobserved` | nessuna riga | ha puntato |

- `blocked_played` e' il numero che conta di piu': **puntate reali che la catena
  nuova avrebbe rifiutato**, col motivo (`by_reason`) e col P/L realizzato (in
  valuta, dal ledger `bets`);
- `would_play_skipped` e' il rovescio: opportunita' che la catena avrebbe
  giocato e la corsia ha saltato — P/L **per unita' di stake** (nessun denaro e'
  stato messo) e `avg_ev`; quando il monitor liquidita' ha uno scarto per quella
  partita, il motivo viene allegato come `hint` (`lane_skip_hints`, fail-safe);
- `unobserved` **NON e' una divergenza**: sono puntate senza riga nella catena
  (valutate fuori dalla finestra di persistenza shadow, aperta il 16/09 alle
  15:00 UTC). Contarle come "bloccate" sarebbe un falso: la catena non le ha mai
  viste. Finiscono fuori da `compared` e la cosa e' dichiarata nel `caveat`.

**`chain_would_play(row)`** = verdetto `approve` **e** stake eseguibile: la
stessa condizione con cui `engine.plan_for_resolved` emette `place_order`. Il
`mode` non entra di proposito — qui si misura se il GATE avrebbe fatto passare il
segnale, non quale comando sarebbe stato emesso in simulazione.

**Garanzie** (tripwire in `test_decision_compare.py`): connessione SQLite
`mode=ro` (**il test tenta un UPDATE e pretende che il DB lo rifiuti**), nessuna
istruzione di scrittura nel sorgente, `import decision.compare` che non carica
`tracker`/`auto_bet`/`bot`/`odds_api`/`sx_signals` (zero crediti, nessuna rete),
fail-safe su DB assente, **file corrotto** (probe `_assert_readable`: senza,
un file non-SQLite verrebbe letto come "nessuna tabella" e la misura sembrerebbe
vuota invece che rotta) e tabelle mancanti (volume di un deploy precedente).

**Comandi e job.**
- CLI: `venv/bin/python -m decision compare [--days N | --all] [--json]`
  (`--days` default = `DECISION_COMPARE_DAYS`, 7 giorni; `--all` = tutto lo
  storico). Exit 1 se la misura non e' disponibile.
- `bot.decision_compare_job` (ogni 6h, `first=900`, `max_instances=1`): logga
  SEMPRE il riepilogo (e' la serie storica della fase) e notifica **solo gli
  admin** — e' materiale di ingegneria, non un segnale per gli iscritti — e solo
  se c'e' almeno una divergenza, con anti-spam 1 alert/giorno (chiave
  `SHADOW_COMPARE`). Zero costi: legge i due ledger locali.
- Env dichiarate in `preserve()` (`.railway/railway.ts`, blocco `decisionEnv`):
  `DECISION_COMPARE_ENABLED` (default ON: sola lettura) e `DECISION_COMPARE_DAYS`.
  `railway config plan` dopo la modifica: **0 to add, 1 to change, 0 to destroy**.

**⚠️ Cosa NON e' ancora giudicabile.** Le righe di `decisions` esistono solo dal
16/09 15:00 UTC: all'apertura della fase il campione e' minuscolo e il verdetto
sul P/L resta sospeso sotto `MIN_RELIABLE_CLOSED` (20 puntate chiuse fra
`both_play` e `blocked_played`), come `league_gate_impact` aveva gia' insegnato
("il P/L non e' conclusivo, il flusso si'"). Il campo `caveat` dichiara sempre il
campione: nessun ROI verra' letto come verita' prima della soglia.

**Test**: `test_decision_compare.py` (**40 verdi, tutti OFFLINE**: ledger SQLite
temporaneo con lo schema di produzione, nessuna rete, nessun provider) +
regressioni verdi (`test_decision_*` = 399, `test_bot`, `test_auto_bet*`,
`test_favourites_only`, `test_risk_guards`, `test_secret_hygiene`,
`test_liquidity_monitor`) e `verify_guardrails.py` con **A–F tutti bloccanti**.
**Bug trovato dai test**: `lane_skip_hints` riceveva `days=0` ("tutto lo
storico") e lo passava al monitor, dove una finestra a 0 taglia OGNI evento (il
cutoff diventa `now`) — gli indizi sparivano in silenzio proprio nel caso "tutto
lo storico". Ora `0` viene tradotto in `None` prima della chiamata.

### Superfici tennis: parser Challenger/ITF/WTA + backfill (16/09, sera)

Direttiva del proprietario: ridurre la percentuale di superfici sconosciute del
sandbox tennis (era 74%: 122/164 osservazioni) aggiornando il parser dei
metadati tornei. Due commit (`011eaa5` parser, `b5634bb` backfill), entrambi
deployati e verificati sul container.

**1) PARSER ESTESO (`tennis_sandbox.detect_surface`).** Le etichette REALI del
ledger `data/tennis_sandbox/ledger.db` non riconosciute erano 7 tornei:
Szczecin, Biella, Tiburon, Rennes, Guangzhou, Phan Thiet, Guadalajara.
Superficie di OGNI torneo verificata su fonte esterna (Wikipedia, campo
"Surface" del box) prima di aggiungerla — mai indovinata: **Szczecin e Biella =
clay** (terra rossa outdoor; per Biella distinta la Challenger ATP 2026
dall'omonimo ITF femminile indoor defunto), **Tiburon/Rennes/Guangzhou/Phan
Thiet/Guadalajara WTA = hard** (Rennes indoor, il resto outdoor). Matching
deterministico invariato (`_SURFACE_KEYWORDS`, pesi 2 torneo/1 parola,
ambiguita' → None); la variante diacritica "Phan Thiết" e' coperta dalla
normalizzazione esistente. Test: `test_challenger_ita_wta` in
test_tennis_sandbox.py + verifica post-deploy sul container (11/11 etichette
OK, incluso il fallback None).

**2) BACKFILL (`tennis_sandbox.backfill_surfaces()` + CLI
`--backfill-surfaces`).** L'ELO impara la superficie dal campo della riga AL
MOMENTO DEL SALDO (`settle`), quindi le righe salvate col parser vecchio e
ANCORA APERE avrebbero continuato a non insegnare nulla alle superfici. Il
backfill riempie `surface` su signals/observations col parser corrente: SOLO
righe vuote, mai una superficie gia' registrata, tornei non riconosciuti
restano ''. Idempotente e fail-safe (DB assente → contatori a zero). **Esito
sul container: 56 segnali + 133 osservazioni aggiornati, 0 sconosciuti**;
secondo giro 0/0/0 (idempotenza verificata sul ledger reale).

**3) COPERTURA DOPO IL BACKFILL (misurata sul container, 16/09 19:42 UTC).**
Sconosciuta **74% → 0%**: clay 65 osservazioni (51 saldate), hard 117 (95
saldate). Totali ledger: 182 osservazioni (146 saldate), 109 segnali, 82
chiusi (29V/51P), ROI −8.83% vs avg_ev +38.47% (la sovrastima EV resta il
problema da tarare, ora misurabile per superficie).

**4) LIMITE NOTO — l'apprendimento superficie-specifico NON e' retroattivo.**
I 146 match gia' saldati lo erano con surface vuota (aggiornavano solo
l'overall): `ratings.json` ha 269 giocatori e **0 rating di superficie**. I
rating per superficie cominciano ad accumularsi dai 36 match ancora aperti
e da quelli futuri (le cui righe ora portano la superficie corretta).
Eventuale ricostruzione storica = replay ordinato di tutte le osservazioni
saldate con ELO da zero (deciso solo se il proprietario lo richiede: ogni
replay sovrascrive la storia dei rating).

### Strategia T-60 + 4 circuit breakers + gate leghe in corsia (17/09/2026)

Direttiva del proprietario: la **decisione esecutiva** di una partita si prende
in una FINESTRA di 10 minuti (T-60..T-50 dal fischio), con micro-allocazioni e
quattro **circuit breakers** attivi PRIMA di qualunque ordine reale. Codice
deployato assieme (commit in corso): `auto_bet.py`, `bot.py`,
`decision/models.py`, `decision/stake_engine.py`, `fixture_engine.py`,
`sx_signals.py`, `conftest.py`, `verify_guardrails.py`,
`.railway/railway.ts`, due file di test NUOVI
(`test_t60_breakers.py`, `test_league_gate.py`).

**1) FINESTRA ESECUTIVA T-60..T-50 (`auto_bet.t60_window`).** Apertura
`T60_WINDOW_MIN_MIN` (60), chiusura `T60_WINDOW_MAX_MIN` (50). Verdetto:
`before` (kickoff oltre i 60'), `within` (finestra), `missed` (< 50' o gia'
iniziata), `unknown` (kickoff non parsabile) — gli ultimi due sono
**fail-closed**: non si ordina. Con `T60_EXECUTION_ONLY` (default **ON**) la
corsia `run_today_bets` **fuori finestra non ordina**: il palinsesto resta
SCANSIONATO e classificato (ledger `predictions` + shadow mode completi),
nessun ordine parte. `T60_EXECUTION_ONLY=0` ripristina l'orizzonte 0.5-24h di
prima (usato dai test e dalla diagnostica). Nuovo job `bot.t60_job` ogni 60s
(`first=75`, `max_instances=1`) che chiama `auto_bet.t60_dispatch_pending()`.

**2) CB1 — HARD CAP PER ORDINE (`T60_MAX_STAKE_USDC`, default 1.00 USDC).**
NESSUN calcolo dinamico (Kelly incluso) puo' produrre uno stake sopra il tetto:
viene **SORSCRITTO** (`decision.stake_engine.size` in `mode='live'`,
`cap_source="t60_hard_cap"`), non negoziato. Il valore 1.00 = minimo ordine
eseguibile SX Bet: con la direttiva letterale 0.50 OGNI ordine sarebbe stato
scartato dal floor e il sistema sarebbe rimasto armato ma inerte
(`T60_MAX_STAKE_USDC=0.50` per tornare alla lettera). `t60_stake()` ignora il
Kelly e applica comunque i cap di portafoglio (correlazione 30%, esposizione
totale 40%) e la cassa reale. `T60_MAX_ODDS` (1.80) chiude il tetto quota.
`decision.models.t60_executable()` e' l'UNICA fonte della regola (usata dal
validatore d'ordine e dai tripwire).

**3) CB2 — KILL SWITCH PATRIMONIALE (`T60_KILL_WALLET_USDC`, default 30.0).**
Equity wallet (liberi + in gioco, MAI il disponibile: l'escrow non e' una
perdita) **≤ 30 USDC → sistema ARRESTATO**: flag persistente sul volume
(`data/execution/t60_kill.json`, scrittura atomica), 0 puntate in QUALUNQUE
modalita' finche' un admin non lo disinnesca. Lettura **fail-closed** (flag
illeggibile = blocco attivo); un wallet **non leggibile** NON arma il flag (un
errore API transitorio non deve arrestare il sistema), ma il dispatch T-60 esce
fail-closed. Alert Telegram di emergenza al primo innesco + promemoria
1/giorno (`bot.t60_kill_watch_job` ogni 6h, chiave `T60_KILL`) + comando admin
**`/t60reset`** (disinnesca, RILEGGE il wallet e si riarma da solo se l'equity
non e' risalita: protegge dal riarmo immediato dopo un top-up dimenticato).

**4) CB3 — CONTRATTO PYDANTIC RIGIDO (`decision.models.T60OrderContract`).**
`extra="forbid"`; `price > 1.0`; `league` NON vuota; **fuso orario
OBBLIGATORIO** su `kickoff`/`created_at`/`validated_at` (mai un istante
ambiguo su un ordine reale); `kickoff > created_at`; `mode='live'` ⇒ provider
presente. Un payload che viola il contratto (o CB1) e' **SCARTATO** e non
corretto in silenzio: `auto_bet.validate_order_payload` + riga sul ledger
`bets` `mode='rejected-t60'` con `stake=0.0` e il motivo — **mai** verso il
provider.

**5) CB4 — GATE DI MERCATO + LIQUIDITA'.** `t60_dispatch_pending` esce
fail-closed se il feed di mercato (SX, 3 refresh conformi) non e' validato
(nessun ordine), e l'esecuzione passa dallo STESSO `_live_fill` del giro
normale (floor EV, size al floor ≥ `max(stake × 2, 25 USDC)`, scarto
registrato in `liquidity_monitor`): la guardia non e' reimplementata, cosi'
non puo' divergere dalla produzione. Esecuzione: righe `decisions`
`approve`+`validated` in finestra, dedup `UNIQUE(match_id, esito)`; `mode=sim`
registra solo paper (`t60-sim`), `mode=live` piazza e scrive la riga `mode='live'`
con `bet_id` reale. Notifica Telegram su ogni ordine T-60 LIVE
(`bot.t60_job`, `mode='t60-live'`).

**6) GATE LEGHE APPLICATO ALLA CORSIA ORDINI (17/09, dopo la misura del
15/09).** La strategia "solo campionati vincenti" (5 leghe) non era applicata
dalla corsia che piazza DAVVERO: i candidati di `fixture_engine` e
`sx_signals` non portavano la chiave `league`, quindi `is_sane(league="")`
trattava la lega vuota come AMMESSA. Ora:
- i candidati di entrambi i motori portano `league` (la classificazione
  per-esito non e' piu' cieca);
- `auto_bet._today_value_picks` riapplica `value_filter.league_allowed`
  (difesa in profondita') ed e' **fail-closed sulla lega assente**: senza
  sapere cosa si sta giocando non si ordina;
- `sx_signals.SX_LEAGUE_ALIASES` copre le varianti con PREFISSO PAESE delle
  leghe della strategia ("England Premier League", "Germany Bundesliga",
  "France Ligue 1", "Netherlands Eredivisie", "Turkish Super Lig", ...):
  un falso DIVIETO su una lega ammessa varrebbe più di un divieto mancante
  (azzererebbe il flusso autorizzato).
⚠️ Conseguenza ATTESA (misurata il 15/09): il gate spegne quasi tutto il
flusso — nelle ultime 72h i segnali giocabili erano 0 nelle leghe ammesse e 5
in leghe vietate. E' la scelta prudente del proprietario, non un bug: per
tornare indietro serve allargare `STRATEGY_LEAGUES`, non togliere il gate.

**7) TEST E VERIFICHE.** `test_t60_breakers.py` (nuovo, 33 verdi: finestra,
CB1 sovrascrittura del Kelly, CB2 flag/persistenza/fail-safe sui wallet non
leggibili, CB3 payload malformato → riga `rejected-t60` e nessuna chiamata al
provider, CB4 dedup/parziale/sim-mai-al-provider); `test_league_gate.py`
(nuovo, 32 verdi). `verify_guardrails.py` ha ora **7 scenari (A-G)** e
l'ultimo giro e' `TUTTI I GUARDRAIL BLOCCANO` (exit 0): F = lega vietata mai
candidata + controprova su lega ammessa, G = fuori finestra T-60 solo
scansione + controprova con `T60_EXECUTION_ONLY=0`, CB1 (bankroll 10000 →
stake 1.00), CB3 (payload stake 5.00 scartato), CB2 (equity 25 → flag armato,
0 ordini). Altri due fix in questo giro:
- `conftest.py` isola `T60_KILL_FILE`/`DAILY_STOP_FILE` nella tmp dei test e
disattiva `T60_EXECUTION_ONLY`: senza isolamento un test con wallet finto
sotto i 30 USDC armava il CB2 sul percorso REALE e arrestava tutti i test
successivi dello stesso processo;
- `test_auto_bet_live.py` neutralizza la soglia CB2 (i suoi wallet finti sono
12.28/3.0 USDC, documentati in AGENTS): con la soglia vera ogni asserzione di
staking avrebbe misurato il kill switch — e i test che attendono `[]`
sarebbero passati per il motivo sbagliato. La soglia vera resta testata in
`test_t60_breakers.py`;
- `test_sx_native_settlement.test_match_recente_non_scade` usava un kickoff
FISSO (2026-09-11): col passare dei giorni e' SCADUTO da solo (6 giorni >
`SX_STALE_DAYS` 5) e falliva senza che nulla fosse rotto. Ora la data e'
RELATIVA a `now` (stessa lezione del 15/09: un test che scade col calendario
arriva sempre nel momento peggiore).

**Env** (dichiarate `preserve()` in `.railway/railway.ts`, blocco accanto a
`STAKE_CAP_HARD`, NON ancora impostate su Railway → valgono i default di
codice): `T60_EXECUTION_ONLY`, `T60_WINDOW_MIN_MIN`, `T60_WINDOW_MAX_MIN`,
`T60_MAX_STAKE_USDC`, `T60_MAX_ODDS`, `T60_KILL_WALLET_USDC`,
`T60_ORDER_VALIDATION`.

**⚠️ Punti aperti (da decidere, non bug):**
1. **Due percorsi esecutivi.** In finestra T-60 ordina la corsia
   `run_today_bets` (Kelly + cap percentuali: con wallet < 100 USDC e
   `STAKE_CAP_HARD=0` il floor 1 USDC prevale, quindi in pratica 1 USDC) e in
   parallelo `t60_dispatch_pending` ordina le righe `decisions` validate (cap
   CB1). Oggi il secondo e' INERTE in produzione (`DECISION_SHADOW_PERSIST`
   default OFF ⇒ nessuna riga `decisions`), quindi l'esecuzione reale resta la
   corsia; il dedup `UNIQUE(match_id, esito)` impedisce il doppio ordine. Da
   decidere: se il CB1 (1 USDC) debba valere anche sulla corsia Kelly (misura
   fatta il 17/09, vedi sotto) e se attivare la persistenza shadow per dare al
   dispatch T-60 le righe da eseguire.
2. **CB2 a 30 USDC con wallet ~36 USDC**: l'arresto scatta dopo ~6 USDC di
   perdite di equity. E' la soglia della direttiva — ma va ricordato che un
   arresto NON si sblocca da solo se il wallet resta sotto soglia
   (`/t60reset` rilegge e si riarma).
3. La misura del gate leghe del 15/09 e' precedente a questo deploy: dopo
   qualche giorno di ledger conviene rimisurare il flusso (quante puntate
   arrivano davvero in finestra T-60 sulle sole leghe ammesse).

#### Misura d'impatto del cap CB1 sulla corsia (17/09/2026, `t60_cap_impact.py`)

Direttiva del proprietario: prima di decidere se il cap CB1 debba valere anche
sulla corsia Kelly che piazza davvero, **misura**. Nuovo strumento di sola
LETTURA (zero ordini, zero crediti, SQLite `mode=ro`, nessuna rete) che usa
l'`adaptive_stake` REALE della produzione — non una formula ricopiata — piu' il
tripwire `test_t60_cap_impact.py` (20 verdi offline: la connessione RIFIUTA una
`UPDATE`, il sorgente non contiene scritture ne' import di rete, le soglie
seguono l'env reale).

**Perche' la risposta dipende dal BANKROLL**: con `STAKE_CAP_HARD=0`
(produzione dal 12/09) il floor dell'exchange (1.00 USDC) **prevale** sul cap
percentuale — sotto il floor lo stake viene ALZATO a 1.00 USDC, che e'
esattamente il cap CB1. Il taglio esiste solo quando il cap percentuale (1%
value/moderate, 2% strong) supera 1.00 USDC.

**Numeri (configurazione di produzione: cap severo OFF, cap 1%/2%, Kelly 5%;
con Kelly dinamico 0.05-0.40 le soglie NON cambiano: il cap percentuale domina):**

| bankroll | value/moderate | strong_value |
|---|---|---|
| 36 (equity reale 17/09) | 1.00 = | 1.00 = |
| 50 | 1.00 = | 1.00 = |
| 75 | 1.00 = | **1.50 → 1.00 (−33%)** |
| 100 | 1.00 = | **2.00 → 1.00 (−50%)** |
| 150 | **1.50 → 1.00 (−33%)** | **3.00 → 1.00 (−67%)** |
| 250 | **2.50 → 1.00 (−60%)** | **5.00 → 1.00 (−80%)** |
| 500 | **5.00 → 1.00 (−80%)** | **10.00 → 1.00 (−90%)** |

**Soglie di rottura**: `strong_value` da **50.25 USDC**, `value`/`moderate` da
**100.50 USDC** (il cap percentuale, appunto).

**VERDETTO**: con l'equity attuale (**36 USDC**) il cap CB1 non cambierebbe
NESSUNA puntata — il floor 1.00 USDC e' gia' il cap: applicarlo oggi sarebbe a
costo ZERO, e il tetto comincerebbe a proteggere quando il wallet cresce (a
100 USDC dimezza i `strong_value`, a 250-500 USDC taglia il 60-90% dello stake
Kelly). Ledger locale al momento della misura: 6 segnali vivi, **0 tagliati**.
Uso: `venv/bin/python t60_cap_impact.py [--bankroll N] [--clv] [--json]
[--db PATH]`.

**CONFERMA SUL CONTAINER (17/09, sola lettura, `railway ssh` + DB `mode=ro`).**
Env di produzione: `STAKE_CAP_HARD=0`, `STAKE_CAP_PCT=0.01`,
`STAKE_CAP_PCT_STRONG=0.02`, `KELLY_MIN/MAX_FRACTION=0.05`, `T60_*` **assenti**
(default di codice) — esattamente la configurazione misurata. Equity reale
**34.8255 USDC** (33.8255 liberi + 1.00 in escrow, da `execution_engine.py
--balance`; `daily_stop.start_bankroll` 34.8255), flag CB2 assente. Segnali
**1X2 vivi: 2** (Europa League `1` @1.7467 strong_value, edge +37.9pp;
La Liga Atlético Madrid @1.54 strong_value, edge +13.4pp): stake della corsia
**1.00 USDC** ciascuno → CB1 1.00 → **0 tagliati**. Verdetto CONFERMATO sui
dati reali: oggi il cap CB1 e' a costo zero, morde da 50.25 USDC (strong) e
100.50 (value/moderate).

#### Bug trovato dalla verifica POST-DEPLOY: scadenza righe in ritardo di ~1 giorno (17/09/2026)

Il controllo dopo il deploy (`GET /api/health` → `settlement.overdue_orphans`)
ha segnalato **2** dove la memoria dice "DEVE restare 0": righe insaldabili piu'
vecchie della soglia non ancora scadute. Causa REALE (misurata sul container,
non ipotizzata) — il contrasto fra i FORMATI di data:
- le date del ledger sono ISO con la **'T'** (`2026-09-12T07:11:39.587521`, a
  volte con 'Z');
- il cutoff `datetime('now', ?)` produce il formato SQLite con lo **SPAZIO**
  (`2026-09-12 10:48:40`);
- il confronto `created_at < datetime('now', ?)` e' quindi fra **STRINGHE**, e
  `'T'` (0x54) > `' '` (0x20): a parita' di giorno la riga risultava piu' NUOVA
  del cutoff e la scadenza slittava (~1 giorno: la riga del 12/09 07:11 non
  scadeva il 17/09 10:48 ma il 18/09).

**Fix** (`tracker.expire_stale_sx_rows`, tutti e tre i confronti): `datetime(col)`
avvolge i valori (`datetime(m.commence_time)`, `datetime(created_at)`) e
normalizza 'T', 'Z' e l'offset (verificato in SQLite 3.45.1). Tripwire:
`test_created_at_iso_con_T_non_ritarda_la_scadenza` e
`test_commence_time_con_Z_non_ritarda_la_scadenza` — **falliscono entrambi
senza il fix** (verificato con `git stash` del solo `tracker.py`). Sul
container le orfane del 12/09 (11 righe) + 13/09 (20) + 14/09 (3) ora scadono
alla soglia vera.

⚠️ **Lezione permanente**: `datetime('now')` NON e' comparabile con le date del
ledger COSI' COME SONO SALVATE — ogni confronto SQL su date va avvolto in
`datetime(...)`.

**Deploy del 17/09 (`c35f0fe`, deployment `e0fa2a44`) verificato**: health 200,
codice nuovo sul container (`T60 cap 1.0`, `exec_only True`, `kill_wallet 30.0`,
CB2 non armato, `t60_stake(34.83)` = 1.0, gate leghe `Premier League True` /
`La Liga False`, `t60_cap_impact` presente con soglia 50.25).

**ESITO DELLA PULIZIA (17/09, deploy `303f933`, deployment `ccdfa195`).**
`overdue_orphans` **2 → 0**: la scadenza con il confronto corretto ha chiuso
subito le righe insaldabili oltre soglia (2 previsioni chiuse come push,
50 previsioni aperte). Residuo rimanente, TUTTO spiegato e in chiusura
automatica, con **0 crediti** di costo atteso (`leagues_to_query: []`):
- **32 orfane** (nessuna riga in `matches`, insaldabili per costruzione):
  9 del 12/09 scadono oggi/stanotte, 20 del 13/09 il 18/09, 3 del 14/09 il
  19/09 — il job `sx_signals_job` (ogni 15') chiama `expire_stale_sx_rows`
  in coda a `settle_sx_bets`, quindi ora scadono PUNTUALI alla soglia;
- **11 previsioni su partite future** (normali);
- **5 refertabili in attesa di risultato**: NON vengono interrogate perche'
  la politica `SETTLEMENT_BETS_ONLY=1` (15/09: "il referto segue il DENARO")
  interroga solo le leghe con una PUNTATA — la loro lega non ne ha, quindi
  restano in telemetria fino alla scadenza (per saldarle servirebbe spendere
  ~2-3 crediti o rimettere `SETTLEMENT_BETS_ONLY=0`: decisione del
  proprietario, non un difetto);
- **3 righe su leghe fuori catalogo** (`Primera A`, `LigaPro`,
  `Primera Nacional`): insaldabili per scelta (the-odds-api non le copre) →
  scadenza;
- **1 bet live aperta** (`sx-L19974965`, Europa League `1`, con `market_id`):
  partita di stasera, si salda col percorso SX-native (gratis).
Nessuna azione manuale residua: la coda si svuota da sola con le scadenze.

### Refertazione su punteggi LIVE: bet saldate a partita in corso (17/09/2026)

**Direttiva del proprietario**: "verifica stasera che la bet live su Europa
League si saldi da sola col percorso SX-native". La verifica ha fatto emergere
un bug GRAVE, diverso da quello atteso, che avrebbe sbagliato il verdetto
della stessa bet di stasera.

**1) SCOPERTA — tre bet del 15/09 saldate ~12 minuti dopo il kickoff.**
Dal ledger di produzione:

| bet | partita | kickoff | saldata | `match_results` | vera finale |
|---|---|---|---|---|---|
| #41 | Liverpool–Tottenham (esito `1`) | 15/09 19:00 | **19:12** | 0-0 → X | **3-1 → 1** (find SX) |
| #40 | (EFL, esito `1`) | 15/09 18:45 | **18:57** | 0-0 → X | — |
| #39 | (EFL, esito `1`) | 15/09 18:45 | **18:57** | 0-1 → 2 | — |

Tutte e tre chiuse ~12' dopo il calcio d'inizio: sono **punteggi live** salvati
come risultati finali. Il verdetto di #41 (persa) e' quindi FALSO — la partita
e' finita 3-1 e il segno `1` era quello giocato.

**2) CAUSA RADICE (`odds_api.match_scores_by_name`).** La cache punteggi
della the-odds-api conteneva **12 partite con `completed: false` e `scores`
popolati** (partite in corso). `match_scores_by_name` — il punto di passaggio
COMUNE di `bot._update_results`, `sx_signals._results_from_the_odds_api` e
`repair_scores` — associava i gol alle squadre **senza controllare
`completed`**: un `0-0` al 12' diventava un risultato finale e `settle_bets`
chiudeva la riga. Non e' un caso isolato: la finestra di refertazione gira
di continuo, quindi QUALSIASI bet la cui partita e' in corso al giro
successivo al kickoff veniva chiusa con un punteggio non definitivo.

**3) FIX (tre guardie, tutte fail-closed).**
- `odds_api.match_scores_by_name`: **prima** di leggere i gol richiede
  `m.get("completed")`. Campo assente = NON conclusa (la riga resta aperta
  invece di ricevere un verdetto su dati non definitivi). Difende in un colpo
  solo i tre chiamanti.
- `sx_signals._results_from_sx` percorso 1 (`markets/find`): nuova guardia di
  conclusione con `gameTime` — l'evento deve avere almeno `SX_LIVE_MIN_AGE_MS`
  (120'), la stessa soglia gia' usata dal percorso 2. Difesa in profondita':
  se SX popolasse i punteggi live anche nella find, la bet non si chiude.
- `sx_signals._results_from_api_football` (fallback): nuovo
  `_fixture_finished(fx)` — si accettano SOLO gli stati API-Football di
  partita conclusa (`FT/AET/PEN/AWD/WO`); in corso (`1H/HT/2H/ET/...`) e non
  iniziate (`NS/TBD`) vengono scartate (stato ignoto o assente -> False).

**4) TEST.** `test_scores_parsing.py`: fixture aggiornate con `completed:
True` (come la API reale) + 3 test nuovi — partita in corso -> `None`,
`completed` mancante -> `None` (fail-closed), e la **regressione end-to-end**
(`_update_results` con payload live 0-0 NON chiude la bet e NON scrive
`match_results`). `test_sx_native_settlement.py`: find su evento a 15' dal
kickoff -> nessun risultato + controprova a 2h (salda normalmente).
Bug collaterale chiuso nello stesso giro: `_seed_inverted` in
`test_scores_parsing` usava un seed FISSO al 02/09 che, superata la finestra
cassa di 14 giorni, faceva fallire da solo il test (stessa trappola di data
del 15/09) — ora le date sono relative a `now`.

**5) DEPLOY E RIPARAZIONE (17/09, eseguiti).** Commit `cc0e93c` deployato su
Railway (health 200). Verifica del fix **sul container**, sui dati REALI
della cache: delle **306 partite non concluse** (molte con punteggi live)
**0** vengono ora refertate per errore, e delle **178 concluse** **0**
vengono perse (nessuna regressione sui risultati veri).

Riparazione delle tre bet eseguita in sola scrittura mirata: i punteggi
VERI presi da `markets/find` (gratis) — #41 Liverpool 3-1 Tottenham, #40
Hibernian 0-1 Kilmarnock, #39 Middlesbrough 2-2 Millwall — riscritti in
`match_results`, poi `settlement_sanity_check()` + `heal_settled_contradictions()`.
Esito: **#41 da `lost -1.00` a `won +0.86`** (era l'unico verdetto
sbagliato; #40 e #39 erano perse anche nella realta', ma ora hanno il
punteggio corretto), piu' la previsione #12731 risaldata. Il P/L live
totale passa da **-14.14 a -12.28 USDC** (24 chiuse).

**6) BET DI STASERA (#42, Celtic–Ferencvaros, kickoff 17/09 19:00 UTC).**
Verificata in stato di attesa: aperta, `market_id` presente, nel set
`_sx_open_matches()` (17 match). Prerequisiti del saldo automatico tutti
verificati sul container: settlement NON in pausa, `SX_NATIVE_SETTLEMENT`
= default ON, `ODDS_API_KEY` presente (gate), `markets/find` risponde sul
suo hash (`status ACTIVE`, punteggi `None` prima del fischio d'inizio),
`sx_signals_job` ogni 15'. Con il fix si saldera' col **punteggio
finale**: il percorso SX-native tace finche' l'evento non ha almeno 120'
di gioco e il percorso the-odds-api non accetta piu' partite non concluse.

### Contratto `WriteCLVCommand` + valutatore CLV puro (17/09/2026, sera)

Direttiva del proprietario: il modulo di valutazione CLV deve calcolare la
differenza di quota **in modo puro** e restituire all'orchestratore
un'istanza di `WriteCLVCommand` (modello Pydantic immutabile) SENZA gestire
la scrittura su database — stessa filosofia del Command pattern del 15/09:
il motore emette comandi, i gateway eseguono.

**1) IL CONTRATTO (`decision/commands.py`).** Nuova classe
`WriteCLVCommand` (frozen, `model_config = {"frozen": True}`) con i soli
campi richiesti: `signal_id`, `market_id` (l'id del match sul ledger, la
chiave di `tracker.save_clv`), `signal_odds`, `closing_odds` (entrambe
`> 1.0`), `timestamp`, `source`. E' il contratto RESTITUITO dal valutatore
all'orchestratore; `WriteCLVPayload` e' il payload del comando generico
(`Command(kind=WRITE_CLV)`) che il `Dispatcher` instrada, con i campi
ricalcati sulla firma di `tracker.save_clv` piu' `outcome` e `source`.
Nuovo `CommandKind.WRITE_CLV = "write_clv"` in CODA a `COMMAND_ORDER`
(dopo le notifiche: il campione CLV e' audit, non denaro — nessun test
preesistente dipende dalla posizione). Fabbrica `write_clv_command()` con
`dedup_key` stabile per (match, esito, quote): lo stesso campione non si
registra due volte. La differenza di quota NON e' un campo del comando: e'
una MISURA (`clv_diff`), calcolata dal valutatore.

**2) IL VALUTATORE PURO (`decision/clv.py`, nuovo).** `ClvInput`
(osservazione, frozen) -> `evaluate_clv()` -> `ClvEvaluation` con
`command` (l'istanza `WriteCLVCommand`) e `dispatch` (il `Command`
per il dispatcher), costruiti INSIEME dagli stessi valori. **Zero DB, zero
rete**: `clv_diff()` delega a `market_calib.clv_raw` (formula UNICA del
progetto, import pigro — nessuna copia della formula). Esiti
machine-readable:
- `ok` -> comando emesso;
- `skipped` -> niente da misurare ANCORA (closing assente = normale prima
  del fischio; segnale assente): NON e' un errore, nessun comando;
- `rejected` -> dati malformati (market_id vuoto, quote <= 1.0, payload
  rifiutato dal contratto, esito vuoto): il comando NON nasce, mai corretto
  in silenzio, MAI un'eccezione verso l'orchestratore.
Flag `single_sample` quando closing == segnale (l'eco che il report esclude
gia' dalle medie CLV). `evaluate_clv_many` fail-safe su lotti ostili
(osservazione che esplode = `rejected`, non traceback). La valutazione
funziona con `sqlite3.connect` avvelenato (tripwire nel test).

**3) LA SCRITTURA STA NEL GATEWAY (`decision/gateways.ClvGateway`,
nuovo).** Gestisce solo `WRITE_CLV`, `audit_only = True` (telemetria di
mercato: il `Dispatcher` la esclude dal conteggio shadow). Writer
iniettabile (i test girano senza DB); il default DELEGA a
`tracker.save_clv` — l'esecutore di produzione gia' testato, mai
reimplementato (stessa regola di `PlaceOrderGateway` verso
`auto_bet._live_fill`), import PIGRO. Convenzione sulla firma a una quota
di `save_clv`: closing == segnale -> `signal_started=True` (seme della
quota segnale), altrimenti aggiorna la chiusura. Fail-safe: payload
incompleto non arriva mai al writer, un writer che esplode diventa
`CommandResult(ok=False)`, mai un'eccezione.

**4) EXPORT (`decision/__init__.py`).** Aggiunti: `clv`,
`WriteCLVCommand`, `WriteCLVPayload`, `write_clv_command`, `ClvGateway`,
`evaluate_clv`, `evaluate_clv_many`, `ClvInput`, `ClvEvaluation`,
`clv_diff`, `STATUS_OK/SKIPPED/REJECTED`.

**5) TEST (`test_decision_clv.py`, 35 verdi, TUTTI offline).** Contratto:
campi esatti, immutabilita' (frozen) del comando E dell'esito, JSON-safe.
Purezza: sqlite3 avvelenato, `import decision.clv` non carica
tracker/auto_bet/bot. Skip/reject: closing mancante, quote <= 1.0, market_id
vuoto, esito vuoto -> `rejected` senza eccezioni; lotto ostile. Coerenza
command/dispatch e dedup_key stabile. Gateway: writer iniettato, seme
iniziale, fail-safe, `audit_only`, default writer verificato su ledger
temporaneo (`temp_db` locale: `signal_quota` intatto + `closing_quota`
aggiornata). Orchestratore: dispatch end-to-end (report `shadow=True` perche'
il gateway e' audit-only, verifica voluta), shadow registra senza scrivere,
`write_clv` senza gateway finisce negli errori (mai silenzioso).
Regressioni verdi: `test_decision_commands`, `test_decision_shadow`,
`test_decision_validation`, `test_clv`, `test_clv_vig_free`,
`test_decision_pipeline`, `test_decision_adapters`, `test_decision_limits`,
`test_decision_compare`, `test_decision_guards`,
`test_decision_observability`, `test_decision_market`, `test_decision_feed`,
`test_decision_review*`, `test_web_api`, `test_secret_hygiene`, `test_bot`,
`test_auto_bet*`, `test_risk_guards`. `compileall` OK.

**6) WIRING IN SHADOW MODE (17/09, stessa sera) — il percorso gira IN
PARALLELO alla catena, mai al posto.** In `decision/shadow.run_shadow` (il
punto dove la catena valuta gia' ogni giro) per OGNI segnale valutato gira
anche il percorso laterale CLV: `evaluate_clv` (puro) -> esecuzione
DIRETTA del comando sul `ClvGateway` + REGISTRAZIONE sullo stesso
`ShadowGateway` del giro (il registro JSONL mostra il `write_clv` che
sarebbe stato scritto, con dedup per `dedup_key`: un campione per misura).
- **Scrittura**: il writer del gateway e' quello SHADOW (evento
  `clv.shadow_sample` negli eventi di osservabilita') — MAI
  `tracker.save_clv` da questo percorso: il ledger `clv_history` resta di
  `fixture_engine` (verificato: 0 righe dopo giri con segnali). Il writer
  REALE si passa solo esplicitamente (`clv_writer=`): e' la via per
  collaudare offline la scrittura vera senza toccare la produzione.
- **Closing**: la quota corrente del feed (lo snapshot in-process del
  refresh forzato dalla catena). Senza feed (es. `DECISION_FEED_ENABLED=0`)
  la valutazione e' `skipped` con motivo `closing_missing`: onesta', non
  errore — il campione si prendera' nei giri col feed validato.
- **Riepilogo**: `out["clv"]` = {enabled, ok, skipped, rejected,
  dispatched, errors, avg_diff} — il confronto col percorso attuale resta
  una MISURA leggibile a colpo d'occhio. Eventi `clv.lateral` (ok/skipped)
  e `clv.lateral_error` con request/trace/span id; span `clv.write` per
  l'esecuzione.
- **Fail-safe**: un'eccezione dentro la valutazione diventa `rejected` +
  `errors` contati — il giro shadow e il job di `auto_bet` non si rompono
  mai per il CLV. Con le puntate ferme (kill switch) il percorso CLV non
  gira affatto: il fail-fast della shadow resta la prima autorita'.
- **Architettura**: niente Dispatcher finto per il percorso laterale — il
  `Dispatcher` instrada PIANI (record + comandi); qui c'e' UN comando e UN
  gateway audit-only, quindi esecuzione diretta `clv_gw.execute(...)`. La
  registrazione nel registro e' delegata allo stesso `ShadowGateway` del
  giro (stesso formato, stessa dedup): zero formato nuovo da mantenere.
- **Interruttore**: `DECISION_CLV_SHADOW` (default ON, env letta a ogni
  giro) oppure `clv_enabled=False` da codice; spento non aggiunge nulla al
  riepilogo (`enabled: False`, contatori a zero).
- **IaC**: `DECISION_CLV_SHADOW` dichiarata `preserve()` nel blocco
  `decisionEnv` di `.railway/railway.ts` (solo servizio `api`).

**7) TEST DEL WIRING (`test_decision_clv_wiring.py`, 14 verdi, TUTTI
offline).** In parallelo mai al posto (0 righe su `clv_history` dopo un
giro con comando emesso); campione nel registro (`by_kind.write_clv`);
puntate ferme -> CLV spento; ok con closing dal feed (evento + diff
esatta); skipped senza closing (motivo `closing_missing`);
tracciabilita' (trace_id + request_id del giro); segnale ostile ->
`rejected` senza traceback e giro intatto; interruttore flag/env; writer
reale SOLO se iniettato esplicitamente; dedup del registro (2 giri -> 1
`write_clv`); media `avg_diff` su piu' segnali. **Bug trovato durante lo
smoke**: la prima versione chiamava un inesistente `dispatcher.execute`
su un `CommandPlan` fabbricato ad hoc — sostituito dall'esecuzione
diretta + registrazione (vedi punto 6): lo smoke ha fatto da prova del
campo, non solo dei numeri.

**8) SMOKE E PRODUZIONE.** Smoke end-to-end: i tre esiti tracciati con
trace_id (ok dispatched con diff 0.0667; skipped con `closing_missing`;
rejected su valutazione ostile), registro con `write_clv: 1` dedup.
Percorso di produzione reale: `auto_bet._shadow_run(mode="live",
bankroll=35.98)` su ledger temporaneo con un segnale aperto -> 1 segnale
valutato, `clv.enabled True`, `skipped=1` (conftest offline senza feed),
`errors=0`, **0 righe su `clv_history`**. **Nessuna regressione sul
flusso `fixture_engine`**: `git diff` vuoto su `fixture_engine.py`/
`sx_signals.py`/`tracker.py` (il `save_clv` di produzione resta in
`fixture_engine._analyze_match`), suite verdi su value_filter/
sx_signals/clv/market_calib/auto_bet*/bot + tutto il pacchetto `decision`.
⚠️ In produzione il CLV ufficiale resta quello di `fixture_engine`:
quello della catena e' la MISURA in parallelo (come la shadow) — la
scrittura vera via `ClvGateway` partirà solo al passo 3 (percorso
Command), dopo il confronto dei registri.

### Scala lo scanner a TUTTI i mercati SX — passo 1: contratti 2.0 + ledger `market_quotes` (18/09/2026)

Direttiva del proprietario: **lo scanner non deve piu' limitarsi al 1X2** — deve
estrarre, salvare e analizzare tutte le opzioni di mercato offerte da SX Bet per
ogni fixture, in tre passi (1 ingestion, 2 batching API, 3 Poisson
multi-mercato). **Questo e' il passo 1**, con il gateway SQLite.

**⚠️ PRIMA DI SCRIVERE: cosa SX pubblica DAVVERO (non a memoria).** Verificato
sulla doc ufficiale (`docs.sx.bet/api-reference/market-types`, letta il 18/09)
**e** con un probe reale su `GET /markets/active` (`sportIds=5`, lettura
pubblica, zero crediti):

| mercato richiesto | tipo SX | linee | attivo sul calcio (18/09) |
|---|---|---|---|
| 1X2 | **1** | no | ✅ 100 mercati |
| Over/Under | **2** | sì (quarter-line) | ✅ 100 |
| Asian Handicap | **3** | sì (quarter-line) | ✅ 100 |
| **BTTS** | **17** | no | ⚠️ tipo ufficiale ESISTE, **0 mercati attivi** |
| Double Chance | **non esiste** | — | derivabile dal 1X2 |
| Risultato Esatto | **non esiste** | — | derivabile da Poisson (passo 3) |

Vivi ma NON modellati (dichiarati in `SX_TYPES_NOT_MODELLED`, con il motivo):
52 = "12 senza pareggio" (100 mercati, liquido — collide con l'alias legacy
`"12"→1X2`, quindi NON modellato), 226, 835, 77, 63. **"Non modellato" non vuol
dire "inesistente"**: chi legge il codice deve sapere cosa manca e perche', senza
ri-scoprirlo. Conseguenza di progetto: OU/AH/BTTS sono **nativi**, DC e CS sono
**derivati** — e il contratto pretende che la provenienza sia dichiarata, non
inventata.

**1) IL REGISTRO DEI MERCATI (`decision/market.py`, schema 2.0).**
`MARKET_SCHEMA_VERSION` 1.0 → **2.0**. `MarketType` (str, Enum: `1X2`, `OU`,
`AH`, `BTTS`, `DC`, `CS`) + `MarketTypeSpec` (frozen) con `selections`,
`has_lines`, `quarter_line_eligible`, `line_bounds`, `native`,
`source_type_ids` (es. `("sxbet", 3)`), `derivable_from`, `requires_score`.
`MARKET_SPECS` e' la **tavola di dati** (niente `if` sparsi); `SUPPORTED_MARKETS`
e `MARKET_SELECTIONS` sono **derivati dal registro** — una fonte sola, cosi' un
mercato nuovo non puo' entrare in un posto e non nell'altro. `spec_for()`,
`market_type_of()`, `SX_TYPE_IDS`, `SX_LINE_BEARING_TYPES`,
`SX_QUARTER_LINE_TYPES` (per il passo 2: decidere `onlyMainLine` e normalizzare
la linea).

**2) `MarketQuote` — le aggiunte sostanziali.** `market_type` (tipizzata,
coerente con `market`), **`line`** (2.5 / -0.75), `main_line`, **`origin`**
(`native` | `derived`), `derived_from`. Due validatori nuovi: `_resolve_market`
(prima dei tipi: `market` e `market_type` che si contraddicono →
`market_type_mismatch`) e `_check_market_shape` (dopo: **linea obbligatoria dove
serve, vietata dove non serve**; quarter-line solo se ammessa; bounds; gli esiti
del Risultato Esatto validati come punteggio). Cross-check mercato/esito
invariato e esteso ai mercati nuovi: e' la classe del caso 09/09 (`over` saldato
su un 1X2).

**3) `FixtureQuotes` — N mercati di UNA partita.** Contenitore con le
INVARIANTI di gruppo: tutte le quote stesso `fixture_id`, kickoff coerente **fra
loro** (non solo col contenitore: coerenza resa simmetrica durante i test),
`fixture_id`/esiti derivabili, `as_rows()` per il ledger. E' l'oggetto che il
feed produrra' per fixture nel passo 2.

**4) `as_row()` / `MARKET_ROW_FIELDS` — il ponte verso il ledger.** 24 campi
piatti, fra cui `line_key` (stringa canonica, `''` per i mercati senza linea),
`ledger_esito` (`"Over 2.5"`, `"Home -0.75"` — **pronto per `ml_audit`**),
`identity_key`, `quote_id`, `derived_from`.

**5) IL LEDGER `tracker.market_quotes`.** Deve rispettare esattamente la
granularita' di `as_row()`: **PRIMARY KEY (fixture_id, market_type, line_key,
selection)** — e' cio' che rende possibili l'inserimento multi-mercato e gli
**upsert continui** quando le quote fluttuano in finestra T-60.
- `MARKET_QUOTE_COLUMNS` e' l'UNICA dichiarazione dello schema (CREATE TABLE,
  migrazione e INSERT leggono tutte da li'); `MARKET_QUOTE_SOURCE` dichiara le
  rinomine (`odds`→`price`, `depth_usdc`→`liquidity`, tuple/dict → JSON) e un
  tripwire pretende che **ogni** campo di `MARKET_ROW_FIELDS` finisca in una
  colonna: un campo nuovo del contratto non puo' sparire in silenzio.
- **`_ensure_market_quotes_table` ordina tabella → colonne → indici** (lezione
  del 14/09 su `decisions`): con gli indici prima delle colonne `_get_conn`
  fallirebbe all'avvio e con lui il bot. La migrazione e' idempotente e le
  colonne di chiave migrate prendono `NOT NULL DEFAULT ''` (⚠️ nella DDL le 4
  colonne di chiave sono `NOT NULL`: **su SQLite un NULL non e' una chiave** —
  i NULL sono distinti fra loro, quindi la tabella ammetterebbe righe "uguali"
  all'infinito).
- Due indici: `idx_market_quotes_lookup` (fixture+mercato+linea, come da
  specifica: il lookup del ciclo auto-bet) **e** `idx_market_quotes_market`
  (mercato+linea, che il composite della chiave non copre perche' comincia dal
  fixture: serve all'audit "tutti gli OU 2.5").
- `save_market_quotes(rows)` = **upsert** `ON CONFLICT(...) DO UPDATE SET` di
  tutto tranne la chiave: una lettura ripetuta del palinsesto aggiorna
  `price`/`updated_at` senza duplicare ne' sollevare violazioni PK. Ritorna
  `{saved, skipped, fixtures, by_reason, error}` e **non solleva mai** (come il
  feedback engine del 14/09: l'ingestione e' telemetria, non deve fermare il
  giro). Difese: righe senza `fixture_id`/`market_type`/`selection`/prezzo
  valido scartate e contate; **due linee MAI fuse nella stessa riga** (un OU 2.5
  e un OU 3.5 collasserebbero sulla stessa chiave e si salderebbe l'esito di un
  mercato col risultato di un altro); lotto ostile che non fa cadere il
  salvataggio. Letture: `quotes_for_fixture()`, `count_market_quotes()`,
  `prune_market_quotes(days)`.

**6) DEVIZIONI DICHIARATE dallo schema proposto** (scelte, non dimenticanze):
- **`price` non ha il vincolo NOT NULL.** Su SQLite un vincolo non si puo'
  AGGIUNGERE a una tabella esistente, quindi su un DB gia' migrato il NOT NULL
  varrebbe solo per i DB freschi: una garanzia che sembra piu' forte di quella
  che e'. Il prezzo si valida al CONFINE (`_market_quote_row` scarta
  `price_non_valido`), dove la difesa vale su ogni DB.
- **`updated_at` e' TEXT con `DEFAULT CURRENT_TIMESTAMP`** (non TIMESTAMP):
  regola del 17/09 — le date del ledger sono testo ISO e **ogni confronto SQL va
  avvolto in `datetime(...)`**. Per le righe migrate da una tabella precedente
  `updated_at` nasce vuoto (ALTER TABLE non accetta default non costanti) e si
  riempie alla prima riscrittura.
- Colonne in piu' rispetto alla specifica (nessuna in meno): `line`,
  `selection_label`, `source`, `gateway_id`, `schema_version`, `observed_at`,
  `kickoff`, `event_name`, `league`, `home`, `away`, `identity_key`, `quote_id`,
  `extra_json` — servono al contratto e all'audit, e il ledger non e' un
  sottoinsieme del contratto.
- `main_line` e' **INTEGER** (0/1/NULL), non `BOOLEAN`: e' l'affinita' nativa di
  SQLite e il valore arriva dal contratto gia' normalizzato a flag.
- **La creazione idempotente e' agganciata a `_get_conn`** (riga accanto a
  `_ensure_decisions_table`, quindi **all'avvio** del bot), non a un gateway: il
  ledger e' di `tracker.py` come tutti gli altri e i gateway di `decision/`
  restano dei semplici esecutori (il nome "CLVStorageGateway" della specifica
  non esiste nel progetto: il gateway del CLV e' `ClvGateway`, e per le quote
  `MarketQuotesGateway`).

**7) IL COMANDO (`decision/commands.py`).** `CommandKind.SAVE_MARKET_QUOTES` +
`SaveQuotesPayload` (almeno una riga: un upsert senza righe non e' un effetto e
non deve nemmeno nascere) + `save_quotes_command(rows, ...)` con `dedup_key` che
**cambia col PREZZO**: lo stesso palinsesto letto due volte non produce due
comandi (il registro shadow non si riempie di ripetizioni), ma un movimento di
quota in finestra T-60 si'.
⚠️ **POSIZIONE CAMBIATA rispetto al CLV del 17/09.** `COMMAND_ORDER` ora e'
`SAVE_MARKET_QUOTES → PERSIST_DECISION → PLACE_ORDER → NOTIFY_OPERATORS →
WRITE_CLV`: la regola e' **"prima l'EVIDENZA, poi l'effetto"** (e' gia' il
motivo per cui `persist_decision` precede `place_order`) e lo snapshot di
mercato e' l'evidenza del PREZZO su cui la decisione e' stata presa. Il CLV, che
si puo' misurare solo a cose fatte, **resta l'ultimo** (la scelta del 17/09 non
cambia). Il tripwire che difende l'invariante e' `test_prima_evidenza_poi_effetto`
(`test_market_quotes_store.py`), e i due test del 17/09 che assumevano
`WRITE_CLV` in coda **restano verdi senza modifiche**.

**8) IL GATEWAY (`decision/gateways.MarketQuotesGateway`).** Scrive su
`market_quotes`; `audit_only = True` (sono **dati** di mercato, non un effetto
sul mondo: il `Dispatcher` non le conta come esecuzione reale). `writer`
iniettabile (i test girano senza DB) e il default DELEGA a
`tracker.save_market_quotes` — l'esecutore di produzione gia' testato, mai
reimplementato (stessa regola di `ClvGateway`→`save_clv` e
`PlaceOrderGateway`→`auto_bet._live_fill`), con **import pigro** di `tracker`
(il tripwire "`import decision` non carica la produzione" resta verde).
⚠️ **NON e' collegato alla shadow mode**: la shadow gira ogni 60s e deve restare
senza scritture; il writer reale si passa esplicitamente, come per il CLV
laterale. Quindi in produzione **zero scritture** finche' il passo 2 non
collega il feed.

**9) TEST.** `test_market_quotes_store.py` (**34 verdi, tutti OFFLINE**: SQLite
temporaneo via monkeypatch di `tracker.DB_PATH`, nessuna rete, nessun provider,
zero crediti): schema/chiave composta/`NOT NULL`/indici, migrazione idempotente
su tabella parziale, upsert che aggiorna senza duplicare, due linee mai fuse,
lotto ostile, copertura di `MARKET_ROW_FIELDS`, gateway (scrittura, `audit_only`,
shadow senza effetti), invariante dell'ordine dei comandi.
`test_decision_market_multi.py` (**144 verdi**) copre il registro dei tipi, le
validazioni incrociate, `FixtureQuotes` e le derivazioni dichiarate;
`test_decision_market.py` (**120**) resta la suite del contratto 1.0→2.0.
Regressioni verdi: **pacchetto `decision` = 812 test** (5'17"), `test_auto_bet*`
+ `test_favourites_only` + `test_sx_signals` + `test_t60_breakers` +
`test_league_gate` (156), `test_bot` + `test_secret_hygiene` + `test_risk_guards`
+ `test_scores_parsing` + `test_sx_native_settlement` +
`test_settlement_watchdog` + `test_liquidity_monitor` (129).
`verify_guardrails.py`: **A–G tutti bloccano** (invariato). `compileall` OK, 0
marker di conflitto. **Due bug reali trovati dai miei stessi test**: in `as_row`
usavo `self.market.value` (refuso, doveva essere `self.market_type.value`) e la
coerenza del kickoff fra quote sorelle non era simmetrica.

**10) PASSO 1 COMPLETO — cosa resta.** Passo **2** (ottimizzazione API): il feed
deve scaricare i book di TUTTI i mercati della partita rispettando i vincoli di
SX — `betGroup` al posto di un `type` per chiamata (⚠️ `type` e `betGroup` sono
**mutuamente esclusivi**), `onlyMainLine` per non scaricare 40 linee di OU,
batching/parallelismo misurato (la `_books_parallel` di `sx_signals` e' il
precedente da riusare, non da riscrivere). Passo **3** (motore matematico):
derivare DC e CS dalla distribuzione di Poisson gia' in `poisson_engine`
(correlazione di Dixon-Coles inclusa), con i mercati NATIVI (OU/AH/BTTS) presi
dal mercato e i DERIVATI dichiarati `origin="derived"` + `derived_from`, cosi'
un esito derivato non si confonde mai con uno osservato.

### Multi-mercato OU/AH ATTIVO: AH live, OU in shadow (19/09/2026)

Direttiva del proprietario: **niente attese** — implementare subito i mercati
Over/Under e Asian Handicap collegando i calcoli di Poisson alla tabella
`market_quotes` e all'executor degli ordini, con una configurazione asimmetrica:

    ENABLE_LIVE_AH=1  -> l'Asian Handicap piazza ORDINI REALI subito;
    ENABLE_LIVE_OU=0  -> l'Over/Under resta SHADOW/TELEMETRIA (il leak storico
                         -6.8% su 924 bet va rimisurato sulla corsia nuova
                         prima di rimetterci denaro).

**Nuovo modulo `multi_market.py`** (top-level, come `sx_signals`), catena in 4 passi:

1. **INGESTIONE** (`ingest`): discovery dei mercati SX **type 2 (OU)** e
   **type 3 (AH)** dall'API PUBBLICA (zero chiavi, zero crediti, zero ordini),
   order book taker con la STESSA lettura di `sx_signals` (`_books_parallel`),
   righe validate dal **contratto 2.0** (`decision.market.parse_quote`) e
   upsert su `tracker.save_market_quotes`. Il ledger `market_quotes` era vuoto:
   senza questo passo il multi-mercato non avrebbe avuto nulla da analizzare.
2. **ANALISI** (`analyze_fixture`): per ogni (mercato, linea) — devig a 2 esiti
   (`market_implied`), prob. del modello **push-aware** e blend
   (`adjusted_probability`) + `is_sane` (gli stessi gate del 1X2, con
   `favourites_only=False` perche' il lato favorito e' selezionato prima:
   mercato a 2 esiti, quota in fascia 1.30-1.80, EV >= 2%, edge >= 3pp,
   libro abbastanza profondo).
3. **LEDGER** (`scan`): le previsioni entrano in `predictions` con mercato
   `OU`/`AH` ed esito in formato ledger (`Over 2.5`, `Home -0.75`), quindi il
   settlement e la calibrazione per mercato li misurano. Vengono registrate SOLO
   le linee che contano (la linea giocabile coi suoi due lati, altrimenti il
   miglior candidato scartato): lo snapshot COMPLETO di tutti i mercati vive in
   `market_quotes`, il ledger previsioni non va riempito di migliaia di righe.
4. **ORDINI** (`live_picks` + `order_target`): `auto_bet.run_today_bets`
   concatena `_today_value_picks()` (1X2) + `_multi_market_picks()`; tutti i
   guardrail esistenti valgono identici (T-60, stop-loss, CB2, cap, gate di
   mercato, dedup `bet_exists_open`). L'unica differenza tra le corsie e' il
   **tipo id + la LINEA**, risolti da `execution_engine.resolve_market_for`.

**Modello push-aware (`poisson_engine.ou_outcome_probs`, nuova)**: gemella di
`ah_outcome_probs`. Ritorna (p_win, p_push, p_lose) e tratta il **push** (linea
INTERA con totale esattamente uguale alla linea: puntata restituita, P/L 0) e le
quarter line come due mezze puntate. L'EV e' esatto,
`EV = p_win x (quota - 1) - p_lose` — non derivato da una probabilita'
approssimata. La probabilita' "efficace" `p_win + 0.5 x p_push` serve solo al
confronto con la prob. fair devigata (edge/blend).

**Settlement OU line-aware (`tracker`)**: `_prediction_outcome`, `_esito_won` e
`_esito_possible` leggevano il 2.5 FISSO; ora `ou_line(esito)` estrae la linea
dal testo (`Over 3.25` -> 3.25, default 2.5 per le righe storiche) e `ou_won`
distingue push (linea intera) da perdita. Comportamento sul 2.5 invariato
(verificato dai test).

**Resolver a linea (`execution_engine`)**: il 1X2 di SX e' 3 mercati binari
type 1; OU/AH sono type 2/3 con LINEA. `SxBetProvider.list_market_catalogue`
accetta ora `market_type_ids` e restituisce `market_type_id` e `line` (letta dal
NOME dell'esito, la fonte piu' affidabile, con fallback sul campo se plausibile).
Nuova `resolve_market_for(...)`: fail-closed su provider non sxbet, type non
mappato, evento non univoco, **linea diversa** (un OU 3.5 non e' un OU 2.5) o
lato non riconoscibile. Il grouping evento (nomi + kickoff) e' stato ESTRATTO in
`_unique_event_markets` e riusato da `resolve_match_market`: due copie
divergerebbero.

**Job**: `bot.multi_market_job` ogni 15' (`MM_ENABLED=0` per spegnerlo), stesso
intervallo dello scan 1X2.

**BUG TROVATO DAI TEST (fixato)**: il refactor di `resolve_match_market` aveva
lasciato l'uso di `hk`/`ak` senza la loro definizione -> `NameError` su OGNI
ordine 1X2 live (`test_auto_bet_live` l'ha colto: 2 test rossi, poi verdi).

**Diagnostica**: CLI `venv/bin/python multi_market.py ingest|scan|picks|report
[--json]` (report = quote sul ledger + previsioni aperte/chiuse + ROI per
mercato, zero crediti).

**Env** (in `preserve()` di `.railway/railway.ts`; default di codice: AH ON, OU
OFF, finestra 24h, 12 linee per mercato): `ENABLE_LIVE_AH`, `ENABLE_LIVE_OU`,
`MM_ENABLED`, `MM_HOURS_AHEAD`, `MM_MAX_LINES_PER_MARKET`, `MM_MAX_RAW_MARKETS`,
`MM_GATEWAY_ID`.

**Test**: `test_multi_market.py` (35 verdi, TUTTI OFFLINE: SQLite temporaneo,
provider e order book finti, zero rete/crediti/ordini) + regressioni verdi:
`test_execution_engine`, `test_auto_bet*`, `test_favourites_only`,
`test_risk_guards`, `test_t60_breakers`, `test_league_gate`,
`test_market_quotes_store`, `test_sx_signals`, `test_bets`, `test_predictions`,
`test_settlement_*`, `test_scores_parsing`, `test_value_filter`,
`test_market_calib`, `test_web_api`, `test_reports`, `test_bot`,
`test_ou_exclusion`, `test_secret_hygiene`; `verify_guardrails.py`:
**A-G tutti bloccano**.

⚠️ **Da sapere in produzione**: (a) l'AH ordina solo se il feed di mercato e'
validato e se il pick e' in finestra T-60, e il floor/la liquidita' restano
quelli di SX (con `STAKE_CAP_HARD=0` lo stake e' il floor 1 USDC); (b) l'OU non
produce ordini finche' `ENABLE_LIVE_OU` resta 0 — i suoi segnali si misurano con
`multi_market.py report`; (c) se `resolve_market_for` non trova la linea, l'ordine
non parte e NON lascia righe sul ledger (fail-closed, nessun falso P/L).

### Volume di scommesse: edge a +2pp, finestra T-120, OU ancora shadow (21/09/2026)

Direttiva del proprietario: **aumentare il volume di puntate giornaliere** (il
sistema e' troppo rigido: 1-2 ordini a settimana) con cinque modifiche
"in sequenza". Prima di applicarle sono state riportate le misure gia' in
questo file: 3 delle 5 contraddicono un dato registrato. Le scelte sono state
confermate esplicitamente dal proprietario con quattro domande.

**1) ✅ APPLICATO — soglia di edge +3pp/+5pp -> +2pp/+4pp** (`market_calib.py`:
`MARKET_EDGE_MIN` e `MARKET_EDGE_MODERATE` 0.03 -> **0.02**, `MARKET_EDGE_STRONG`
0.05 -> **0.04**). `value_filter.DEFAULT_LEAGUE_STRATEGY.min_edge` allineato a
0.02 (era l'unico posto ancora a 3pp). ⚠️ E' la **direzione opposta** alla
prudenza dell'11/09, che era stata ripristinata il 13/09 dopo il commit
"FREQUENZA boost": qui la scelta e' **tracciata e voluta**, non un guadagno
tecnico. Il caso `is_sane(prob .62, quota 1.65, market_prob .60)` (edge +2pp)
passa **ora** e veniva respinto prima.
- **Tripwire riallineato di proposito**: `test_risk_guards.TestFasciaFavoriti`
  ora asserisce **uguaglianza esatta** (`== 0.02` / `== 0.04`) e
  `test_edge_sotto_2pp_bocciato` (+1pp respinto, +2pp passa); `test_value_filter`
  due assert aggiornati. L'uguaglianza e' la protezione: un ulteriore
  abbassamento deve essere una decisione tracciata qui, non un silenzio.
- **Telemetria/testi derivati dalle costanti** (lezione 13/09 sui testi
  stantii): `performance_report._edge_analysis` legge i bucket da
  `MARKET_EDGE_STRONG`/`MARKET_EDGE_MODERATE` (erano 0.05/0.03 in SQL) e il
  report stampa le soglie reali; `backtest._edge_split` usa `MARKET_EDGE_MIN`
  (era 0.03); `bot.py` (blocco 🛡 Filtri Pro), `fixture_engine.py` (riga filtri
  schedina), `liquidity_impact.py` (sensibilita' + marcatore "attuale" + testo
  finale), `STRATEGY.md` e le due pagine webapp aggiornati.
- **Test**: `test_performance_report.TestEdgeAnalysis` derivava gli edge dei
  bucket da valori fissi (0.08/0.04/0.01) e **sarebbe scaduto in silenzio** al
  cambio di soglia: ora li calcola dalle costanti (stessa lezione delle date
  relative del 15/09).
- **NON toccato**: `market_diagnose.GAP_PP` resta 3.0 — e' la soglia di rumore
  ROI-vs-EV (diagnosi), non il gate di edge: il commento ora lo dichiara.

**2) ⛔ NON applicato — `ENABLE_LIVE_OU=1` (OU resta SHADOW).** Il proprietario
ha scelto di **rimisurare prima**: il leak storico e' **-6.8% su 924 bet**
(06/09) e il backtest del 12/09 da' `by_market` OU **-6.85%**; la direttiva del
19/09 imponeva lo shadow "prima di rimetterci denaro". I segnali OU continuano a
generarsi e registrarsi (telemetria misurabile con `multi_market.py report`).

**3) ⛔ NON applicato — liquidita' AH 25 -> 10 USDC (resta 25 su TUTTE le
corsie).** La misura dell'11/09 con `liquidity_impact.py` dice che a **25 USDC
passa il 100% dei favoriti (42/42)** e a 50 USDC 41/42 (size al floor: p10 102,
p50 263, p90 986 USDC): abbassare a 10 darebbe **guadagno di volume zero**
togliendo la protezione da slippage. In piu' `SX_MIN_EXEC_DEPTH_USDC` e' **una
sola env** condivisa da `sx_signals` (scan), `auto_bet` (ordine),
`multi_market` e `decision.limits`: la si abbasserebbe anche per il 1X2 e per
la catena `decision`, non solo per l'AH.

**4) ⚠️ DA APPLICARE IN PRODUZIONE — finestra esecutiva T-60 -> T-120**
(`T60_WINDOW_MIN_MIN=120`, chiusura `T60_WINDOW_MAX_MIN=50` invariata). E' la
sola delle cinque con effetto reale sul volume senza indebolire un guardrail
misurato: il verdetto `missed` e' fail-closed e `sx_signals_job` (ogni 15',
~4-5 min per giro) con `max_instances=1` puo' far saltare la finestra di 10
minuti. Parametro **solo env** (gia' in `preserve()`), nessuna modifica di
codice; in locale valgono i default 60/50 e `test_t60_breakers` resta verde.

**5) ⚠️ DA ESEGUIRE — scansione `multi_market.py`** per contare i pick
giocabili sbloccati. ⚠️ Va eseguita **sul container**: il DB locale ha 0 righe
in `team_ratings` (i rating vivono sul volume Railway), quindi il gate modello
qui misura il profilo NEUTRO e non e' rappresentativo (caveat del misuratore,
11/09).

**Il vero collo di bottiglia NON sono queste soglie.** Per "1-2 ordini a
settimana" la misura del 15/09 indicava: **gate leghe** (`STRATEGY_LEAGUES`, 5
campionati) → **0 pick giocabili nelle leghe ammesse** su 72h, con le 5
giocabili tutte in leghe vietate; e il **tetto CB1 `T60_MAX_STAKE_USDC=1.00`**
(con `STAKE_CAP_HARD=0` il floor 1 USDC prevale). Se l'obiettivo e' la
**crescita del capitale**, il volume di pick a 1 USDC non la cambia: le leve
sono il gate leghe e la dimensione per ordine, non la liquidita'/edge.

**Verifica locale eseguita**: 400 test verdi nei due lotti mirati
(`test_risk_guards`, `test_value_filter`, `test_market_calib`,
`test_performance_report`, `test_decision_pipeline`, `test_decision_limits`,
`test_multi_market`, `test_t60_breakers`, `test_league_gate`, `test_bot`,
`test_sx_signals`, `test_backtest`, `test_liquidity_impact`, `test_auto_bet*`,
`test_favourites_only`, `test_secret_hygiene`, `test_tier`),
`verify_guardrails.py` **A-G tutti bloccano** (exit 0), 0 marker di conflitto,
`compileall` OK.

#### Esito del deploy + scansione (21/09/2026, 01:53 UTC)

**Deploy verificato**: commit `d98889c` → deployment `6251b209-00d6-498f-852a-fb5487b1cbf4`
**SUCCESS**, `/api/health` 200. Sul container i parametri sono ATTIVI:
`MARKET_EDGE_MIN 0.02` / `MODERATE 0.02` / `STRONG 0.04`, fallback lega 0.02,
`T60_WINDOW_MIN_MIN 120 -> MAX 50` con `exec_only True`, `ENABLE_LIVE_OU False`
(shadow, come deciso), `ENABLE_LIVE_AH True`, `MIN_EXEC_DEPTH_USDC 25.0`
(invariata). `ENABLE_LIVE_OU` e `SX_MIN_EXEC_DEPTH_USDC` **non sono impostate**
su Railway → valgono i default di codice.

**Scansione `multi_market.py` sul container** (`ingest` → `scan` → `picks`):
ingest **171 mercati SX → 225 quote salvate** (4 scartate, **11 fixture**);
scan **10 fixture con quote → 0 SEGNALI GIOCABILI** (0 nelle corsie live AH);
`picks` = `[]`.

**PERCHE' 0 — il collo di bottiglia e' il GATE LEGHE, non edge/liquidita'.**
Diagnostica in sola lettura (`analyze_fixture` sulle 10 fixture): **218
candidati su 218 respinti** e il motivo e' **sempre lo stesso** — «lega ...
esclusa per ROI negativo (strategia solo campionati vincenti)». Le 10 fixture
stanno in **Liga MX (1), Liga Profesional (3), Primera Nacional (2), LigaPro
(2), Brasileiro Serie B (2)**: nessuna in `STRATEGY_LEAGUES`. Conferma sul
campo la misura del 15/09. ⚠️ Su questo campione **non si e' attivato nemmeno
un rifiuto per edge/EV o liquidita'**: abbassare la soglia di edge a +2pp e la
liquidita' a 10 USDC non avrebbe sbloccato UN pick in piu'.

**⚠️ CORREZIONE IMPORTANTE — il report OU/AH era fuorviante.** `shadow_report()`
conta TUTTE le righe di `predictions` per mercato, **anche quelle `rejected`**:
l'ROI che stampava (OU **-8.40%**, AH **-12.42%**) e' quindi dominato dalla
telemetria dei candidati scartati, NON dal P/L dei segnali giocabili. Split
reale per `status` (profit = per unita' di stake, dal ledger):

| mercato | status | righe | chiuse | profit/unit | ROI |
|---|---|---|---|---|---|
| AH | rejected | 144 | 40 | **-6.8615** | -17.2% |
| AH | strong_value | 4 | 3 | **+1.5189** | +50.6% |
| OU | rejected | 147 | 44 | **-12.1168** | -27.5% |
| OU | strong_value | 20 | 19 | **+0.4175** | +2.2% |
| OU | value | 10 | 10 | **+5.5700** | +55.7% |

I **giocabili** sono quindi POSITIVI (OU value+strong: 29 chiuse, +5.99 →
+20.6%; AH strong: 3 chiuse, +50.6%), ma i campioni sono **sotto la soglia di
affidabilita' (30)** che il progetto si e' dato: da rimisurare prima di
qualunque decisione. Da notare che il "leak OU -6.8%" citato come motivo dello
shadow riguardava il mercato OU della 1X2-era, non la corsia multi-mercato:
qui il segnale OU e' positivo su 29 chiuse. **Prossimo passo naturale**:
aggiungere lo split per `status` a `shadow_report()`/`format_report()` (oggi
mostra un numero che somma giocabili e scartati) e continuare a raccogliere
campione su OU e AH **prima** di aprire l'OU.

### Falso stop-loss del 21/09/2026: due basi di misura confrontate (fixato)

**Sintomo**: alle 15:53 UTC `data/execution/daily_stop.json` registrava
`equity wallet -40.4% dall'inizio giornata (valore 20.00)` e il blocco di 24h
(fino al 22/09 15:53). Il wallet era INTATTO.

**Verifica sul container (sola lettura)**: `execution_engine.py --balance` →
`availableBalance 33.5535`, `exposure 0`, escrow 0 — esattamente il
`start_bankroll` del giorno (33.5535). **Zero bet piazzate il 21/09.** I log del
giro (ogni 60s) mostrano `bankroll LIVE = equity 33.55 USDC` e subito dopo
`STOP-LOSS GIORNALIERO attivo`: il blocco era un fantasma.

**Causa radice**: due BASI di misura diverse confrontate fra loro. Il
riferimento del giorno era stato preso dall'EQUITY del wallet (33.5535); quando
la lettura del wallet SX e' FALLITA, `run_today_bets` e' ripiegato sulla cassa
simulata (`bankroll_stats()` → **20.0**) e `check_daily_stop` ha confrontato
20.00 contro 33.5535 → -40.4% inesistente. E' la stessa CLASSE di bug del
15/09 (disponibile vs equity), con un'altra coppia di grandezze: un errore di
rete non e' una perdita.

**Post-mortem sul ledger — NESSUNA sovraesposizione**: 27 bet live totali,
stake **massimo 1.00 USDC** (floor SX; una partial da 0.9253), 26.93 USDC
piazzati dal 09/09, P/L live cumulato **-13.54 USDC**. Le perdite grosse sono
puntate da 1 USDC a quota 3-4.6 del 09-10/09, **prima** della strategia
favoriti netti: ne' il Kelly ne' le quote hanno mai sovraesposto un singolo
evento (il cap e il floor hanno retto).

**Fix** (`auto_bet.py`): `check_daily_stop(bankroll, basis, basis_key)` non
confronta MAI letture di basi diverse. `BASIS_PRIORITY` = `live_equity`(2) >
`cassa`(1): una base piu' autorevole **ri-arma** il riferimento del giorno,
una meno autorevole viene **ignorata e loggata** (nessun trigger). Il chiamante
dichiara la base reale (`_wallet_equity is not None` → `live_equity`, altrimenti
`cassa`), cosi' il log non mente piu' ("equity wallet" su una lettura di
cassa). `daily_stop_status()` espone `basis_key`.

**Test**: `test_risk_guards.TestStopLossGiornaliero` (regressione 21/09,
upgrade a base piu' autorevole, nessuna declassazione, stessa base che triggera
ancora) + `test_auto_bet_live.TestBaseStopLossDichiarata` (il chiamante dichiara
`live_equity`/`cassa`). Verificati: 52 test sui due file, 142 sul giro
regressioni, `verify_guardrails.py` **A-G bloccano tutte**, 0 marker di
conflitto, `compileall` OK.

**⚠️ Da NON fare**: forzare un `clear` manuale prima di aver capito la causa —
era proprio il rischio che il post-mortem ha evitato. Lo stop si sarebbe
comunque scaduto alle 22/09 15:53.

**Crediti the-odds-api (21/09)**: `GET /api/credits` → `status: critical`,
`remaining: 1`, consumo misurato **62.4/giorno** su finestra 20h,
`days_to_reset: 9` (reset 01/10) → 0.1/giorno sostenibili. `should_query_sport`
sotto 15 crediti riduce la rotazione alle 3 leghe di emergenza ma **NON la
ferma**: senza pausa (env) o chiave di backup la rotazione continua a chiamare
fino a esaurire anche l'ultimo credito (e li' iniziano gli errori HTTP nei log).

**Hard limit crediti — sotto 5 NIENTE HTTP (21/09/2026, direttiva del
proprietario)**: `odds_api.CREDIT_HARD_STOP` (env `ODDS_CREDIT_HARD_STOP`,
default **5**) + `credits_hard_stopped()`. Sotto soglia **nessuna chiamata HTTP
verso the-odds-api**, indipendentemente dalla rotazione ridotta
(`should_query_sport` e' irrilevante). Gate nei DUE soli punti che fanno HTTP:
`_get_odds` (rotazione quote → ritorna `[], 0`) e `fetch_scores` (settlement →
ritorna i punteggi GIA' in cache, mai dati inventati). Fail-open sull'assenza di
telemetria (nessuna cache `toa_*.json`), stessa direzione di `should_query_sport`;
il warning esce UNA volta per processo. **Secondo fail-safe — telemetria
VECCHIA**: una cache sotto soglia piu' vecchia di `CREDIT_HARD_STOP_MAX_AGE_H`
(env `ODDS_CREDIT_PROBE_HOURS`, default 6h) NON blocca. La cache si aggiorna
solo con una chiamata e le chiamate sono bloccate: senza questa via d'uscita
una **chiave sostituita o il reset del 1° ottobre resterebbero invisibili per
sempre** (blocco eterno). Passa UN probe, che la risposta 429 riporta a costo
ZERO crediti. Telemetria senza timestamp -> non si blocca (eta' ignota). Test in `test_odds_api.py`:
soglia (4 → blocco, 5 → no, telemetria assente → no), costante configurabile,
rotazione bloccata + controprova a 50 crediti, settlement su cache +
controprova. ⚠️ `surebet_engine.py` resta **INDIPENDENTE per design** (tripwire:
mai import da tracker/bot): ha la sua guardia `SUREBET_MIN_REMAINING` (50).

**Stato crediti verificato sul container il 21/09 (sera)**: `ODDS_API_KEY` e'
ANCORA quella vecchia (len 32, sha12 `5c483976d988`, invariata dal 12/09) → la
chiave di backup annunciata dal proprietario **NON e' arrivata sul servizio
`api`** (`railway variables --service api --set ODDS_API_KEY=...`). La
telemetria piu' recente e' del **20/09 15:32** (Eliteserien, `remaining: 1`):
e' ~29h vecchia, quindi con il hard stop scatta la regola di PROBE (vedi
sopra) e il sistema non resta mai cieco. ⚠️ Senza la regola di probe la chiave
nuova non sarebbe MAI stata vista (la cache non si aggiorna senza chiamate).

**Template Telegram "BOT - TRADING - CRYPTO" — NON ESISTE (chiarimento 21/09):
il riferimento era a un ALTRO progetto, va ignorato.** In questo repository non
c'e' alcun bot di crypto trading: la parola "crypto" compare solo per dire che
**SX Bet e' un exchange P2P crypto** (`execution_engine.py`, DEPLOY.md) — e'
la rete su cui girano gli ordini, non un'attivita' di trading di criptovalute.
Conseguenze operative permanenti:
- le uniche uscite Telegram sono **messaggi di TESTO costruiti nel codice**
  (`bot.py`, `surebet_engine.py`, `tennis_sandbox.py`): nessun template
  esterno, nessun header di intestazione, nessun riferimento a crypto/trading;
- l'unica integrazione esterna e' il **webhook n8n opzionale** di
  `surebet_engine.py` (`SUREBET_WEBHOOK_URL`, `_send_webhook`), che invia il
  payload di `build_json_payload(opp)` — nessuna intestazione, nessun titolo;
- un header "BOT - TRADING - CRYPTO" puo' stare SOLO nel workflow n8n esterno
  o nello script di broadcast, cioe' **fuori da questo repository**.
**Se si mette mano alle notifiche, il perimetro di lavoro e' esclusivamente**:
(a) la formattazione delle stringhe di testo (`format_telegram_alert` e
`build_inline_keyboard` in `surebet_engine.py`; i testi dei messaggi di
`bot.py` per la parte bot) e (b) il payload del webhook
(`build_json_payload`). Niente template, niente crypto: qui si editano
stringhe e JSON.
⚠️ **Nome del file**: e' `surebet_engine.py` (non `surebetengine.py`) — il
modulo indipendente di arbitraggio descritto nella sezione "Surebet engine
indipendente".

### Monitoraggio post-riavvio: chiave nuova OK, stop-loss ANCORA armato + catena cieca (21/09/2026, sera)

**1) CHIAVE NUOVA VERIFICATA E FUNZIONANTE (zero 429/403).** Impronta letta sul
container: `ODDS_API_KEY` len **32**, sha12 **`6de9e6433715`** (la vecchia era
`5c483976d988`) → la chiave e' arrivata sul servizio `api`. Prova live: il giro
di settlement delle 20:50 UTC ha fatto ~21 chiamate `/scores` con
`crediti residui` da **496 a 456** (40 crediti, ~2/chiamata) e **0 occorrenze di
`429`/`403`/`unauthorized`/`too many`** in tutta la finestra di log. Il deploy
`a91200e4` (21/09 20:48 UTC) e' **SUCCESS**, health 200, `railway list` mostra un
solo progetto (`quotaverace`: l'orfano e' stato eliminato il 12/09).
**Il fix del probe (commit `c1918dc`) si e' VISTO in produzione**: alle 20:50:09
il log recita `crediti 1 < soglia 5 ma telemetria vecchia (29.3h) — un PROBE per
rileggere i crediti` e subito dopo la lettura e' 496 — senza quella via d'uscita
la chiave nuova sarebbe rimasta invisibile per sempre (la cache si aggiorna solo
chiamando, e le chiamate erano bloccate).
⚠️ Effetto collaterale ATTESO: con i crediti di nuovo sani
`heal_skipped_low_credits` e' **false**, quindi la verifica periodica delle leghe
senza righe aperte (36h) e' tornata attiva: ~20 leghe x 2 crediti ogni 36h ≈ 27
crediti/giorno, sopra l'euristica di 25/giorno ma sotto i **50,7/giorno
sostenibili** fino al reset (01/10, `days_to_reset: 9`). Da tenere d'occhio col
credit watchdog (ogni 6h).

**2) IL CICLO BASATO SULL'EQUITY E' RIPARTITO — ma le puntate NO.** Ogni 60s il
log mostra `auto_bet: bankroll LIVE = equity 33.55 USDC (disponibile 33.55 + in
gioco 0.00)`: la base EQUITY del 15/09 funziona e il wallet e' leggibile. Subito
dopo, pero': `auto_bet - ERROR - STOP-LOSS GIORNALIERO attivo fino a
2026-09-22T15:53:37 — nessuna puntata`.
`data/execution/daily_stop.json` era **ANCORA PRESENTE sul volume** (mtime 21/09
15:53, `start_bankroll 33.5535`, `reason "equity wallet -40.4% ... (valore
20.00)"`): e' il blocco FANTASMA documentato sopra (cassa 20.0 confrontata con
the equity 33.5535). Il fix `9468eb1` impedisce nuovi inneschi falsi ma
**rispetta un blocco gia' attivo**: per ripartire subito va rimosso a mano
(`clear_daily_stop()` / `rm data/execution/daily_stop.json`), altrimenti scade da
solo il **22/09 15:53 UTC**. **Il blocco e' stato poi rimosso a mano alle 21:40
UTC**, su decisione esplicita del proprietario (la causa radice era gia'
compresa e corretta): vedi la sezione di deploy qui sotto. Gli altri guardrail
sono a posto: kill switch `live` + `provider_ready: true`, `settlement_paused:
false`, `t60_kill.json` ASSENTE (equity 33.55 > 30).

**3) BUG TROVATO DAL MONITORAGGIO — LA CATENA ERA CIECA ALLO STOP-LOSS**
(`decision/kill_switch.py`, fixato). `kill_switch.status()` leggeva
`stop.get("active")` mentre `auto_bet.daily_stop_status()` espone la chiave
**`stopped`** → `daily_stop_active` era **sempre False** nel fail-fast.
Prova raccolta in produzione: `python3 -m decision status` rispondeva
`stadio betting : libero` mentre `auto_bet` bloccava OGNI giro. Raggio d'azione:
`require_clear`/`SafetyBlockError`, l'uscita anticipata della shadow mode, il
messaggio di `/autobet` e (in futuro) il percorso Command — tutti ciechi allo
stop-loss giornaliero. **Nessun rischio di denaro immediato**: la corsia che
ordina (`auto_bet.run_today_bets`) legge il file per conto suo, e il dispatch
T-60 e' inerte (nessuna riga `decisions` validata). Fix:
`stop.get("stopped", stop.get("active"))` + dettaglio da `reason` — le sonde
iniettate nei test con la chiave `active` continuano a funzionare.
Tripwire: `test_decision_pipeline.TestKillSwitch.test_sonda_reale_dello_stop_loss_giornaliero`
(usa la sonda VERA, non un probe) — **fallisce senza il fix** (verificato con
`git stash` del solo `kill_switch.py`) — e
`test_stop_loss_scaduto_non_blocca` (file con `stopped_until` nel passato).
**Lezione**: un tripwire che inietta l'istantanea (`daily_stop_active=True`) non
protegge la SONDA che la costruisce; per le autorita' di sicurezza va testato
anche il percorso reale file → stato.

**4) ALTRE OSSERVAZIONI (nessuna azione richiesta).** Al boot:
`Token o chat_id Telegram mancanti, messaggio non inviato` (una volta sola, il
resto delle notifiche parte); `sx_signals: settlement — leghe non mappate a
SPORTS_MAP` (First League, LigaPro, Primera A, Primera Nacional: insaldabili per
scelta, documentate); `decision.feeds: riuso dello stato per sxbet-feed (17 quote
non disponibili in questo processo)` ad ogni `t60_job` — il gate resta validato,
da riverificare se il dispatch T-60 verra' armato davvero.
Analisi 1X2 del giro: 6 partite, **0 segnali value** (EV da -2.3% a -18.0%: i
respinti sono per EV negativo, non per il gate leghe); `multi_market`: 6 fixture
ingerite, **0 segnali giocabili** (0 nelle corsie AH). Il collo di bottiglia
resta quello misurato il 15/09 e il 21/09 01:53: gate leghe + soglie, non i
crediti.

**5) TEST ESEGUITI IN LOCALE**: `test_decision_pipeline` (51),
`test_decision_guards`/`commands`/`shadow`/`validation`/`adapters` (156),
`test_risk_guards` + `test_auto_bet_live` + `test_settlement_pause` (61) — tutti
**verdi**.

**6) DEPLOY DEL FIX + SBLOCCA DELLO STOP-LOSS FANTASMA (21/09/2026, 21:27 UTC).**
Commit **`9365f2a`** (`fix(decision-chain): corretta rilevazione dello stato
stopped per lo stop-loss`) → push su `main` → deployment
**`76300347-10f5-4737-9aa0-a196aeea8c06` SUCCESS** (21:26:55, health 200).
Prima del push: nessun marker di conflitto, `compileall` OK, **232 test verdi**
nei lotti mirati (`test_decision_pipeline`/`guards`/`risk_guards` = 91 +
`test_auto_bet*`/`t60_breakers`/`league_gate` = 141).
**Il fix e' VISIBILE IN PRODUZIONE**: `python3 -m decision status` sul container
ora dichiara `stadio betting : BLOCCATO stop-loss giornaliero attivo: equity
wallet -40.4% ...` (prima: `libero` con `auto_bet` che bloccava ogni giro).
**Sblocco eseguito alle 21:40 UTC** su decisione del proprietario (chiamata
`ask_user`: la causa era compresa e corretta, il blocco era provatamente
fantasma): `auto_bet.clear_daily_stop()` → `ls` = file assente,
`daily_stop_status()` = `stopped False`. Il file viene poi **ricreato dal primo
giro come semplice RIFERIMENTO del giorno** (`{"day": "2026-09-21",
"start_bankroll": 33.5535, "stopped_until": null, "basis_key": "live_equity"}`,
mtime 21:40:57): e' il comportamento corretto, non un riarmo.
**Cicli successivi (21:40:57 / 21:41:57 / 21:42:57) tutti PULITI**: bankroll
`equity 33.55 USDC (disponibile 33.55 + in gioco 0.00)`, strategia favoriti
netti, corsie multi-mercato (AH live, OU shadow), `0 puntate piazzate` e **0
errori** (nessun `429`/`403`/traceback). Il gate di mercato NON blocca: il feed
SX e' validato (`feed: riuso dello stato per sxbet-feed`).
**Perche' 0 puntate**: non e' un blocco ma l'assenza di candidati — 0 righe
`predictions` 1X2 aperte con status value/strong_value/moderate,
`_today_value_picks()` = 0 e `_multi_market_picks()` = 0 (la corsia e' viva e
valuta, ma non c'e' nulla da giocare a quest'ora: stesso collo di bottiglia di
gate-leghe/soglie misurato il 15/09 e il 21/09). Crediti **456** (chiave nuova,
`budget alert: False`), `bets` 44 (0 aperte), `predictions` 132 aperte,
`decisions` 4 righe.

### Allentamento del League Gate: Tier-2 in probation + copertura analisi (21/09/2026, notte)

**Direttiva del proprietario**: allentare il gate di lega per aumentare il
volume operativo (era ~1-2 ordini/giorno), con le protezioni anti-spread e
anti-quote-spazzatura intatte. Chiesto prima di scrivere codice: analisi della
configurazione, misura dei blocchi reali, proposta; poi scelta A+B+C.

**LA MISURA HA CORRETTO LA PREMESSA.** Sul ledger di produzione (sola lettura,
`mode=ro`, zero crediti):
- **scarti SOLO-lega nelle ultime 24h: 0** (72h: 3 | 7gg: 5);
- **scarti liquidita' in 24h: 1** (`depth_totale`);
- nelle leghe AMMESSE, 12 righe respinte in 7 giorni: **tutte e 12 per EV < 2%**
  (0 per edge, 0 per fascia quota, 0 per favourite gate) → abbassare l'edge non
  avrebbe sbloccato nulla dove la strategia e' validata;
- il **board** aveva **6 partite in 7 giorni** e le analisi erano **26 oggi
  contro 137 il 20/09 e 104 il 19/09**.
**La causa dominante era `ODDS_DAILY_BUDGET=2`** (default di codice 12),
eredita' della crisi crediti del 12-21/09: 2 sole leghe/giorno di rotazione →
quasi nessun candidato a valle. I crediti ora sono sani (**456**, ~50,7/giorno
sostenibili fino al reset dell'01/10).

**1) OPZIONE A — RIPRISTINO DELLA COPERTURA (env, zero codice).**
`ODDS_DAILY_BUDGET` **2 → 8** e `SETTLEMENT_HEAL_INTERVAL_HOURS` **36h → 48h**
(la verifica periodica delle leghe senza righe aperte lascia spazio alla
rotazione; costo atteso ~28 crediti/giorno su ~50 disponibili). Impostate su
Railway con `railway variables --set` e **dichiarate `preserve()`** in
`.railway/railway.ts` (con `SETTLEMENT_WINDOW_DAYS`). Effetto atteso sui dati
del 20/09: da 3 a ~30-45 candidati 1X2/giorno.

**2) OPZIONE B — GATE A TRE STATI: core / probation / bloccata**
(`value_filter.py`). Non piu' binario: `PROBATION_LEAGUES` (15 leghe) sono
giocabili con strategia **piu' severa** del core — `PROBATION_STRATEGY`
= `min_edge 0.04`, `kelly_mult 0.4`, `max_stake 0.5%` — mentre restano
**vietate** le 5 leghe con ROI misurato negativo (Serie A −5.9%, La Liga
−6.3%, Belgian Pro League −6.3%, Liga Portugal −13.4%, Greek Super League
−69.4%). Nuova `league_tier()` per log/telemetria; `league_allowed()` e
`get_league_strategy()` includono il tier; `is_sane` applica l'edge del tier
automaticamente (nessun chiamante passa `market_edge_min`).
- **Tier-2 ammesso**: EFL Championship, Serie B, MLS, Brasileirao, Argentina
  Primera, Swiss Super League, Eliteserien, Austrian Bundesliga, Scottish
  Premiership, Superliga Danimarca, Allsvenskan, K League 1, J1 League,
  Liga MX, Saudi Pro League. Criteri: in `SPORTS_MAP` (saldabile), scansionata
  da SX, nessun ROI misurato negativo.
- **Eredivisie resta CORE** (−1.8%, n=23: differenza dentro il rumore —
  decisione esplicita del proprietario, non un errore di trasferimento).
- **Alias SX estesi** (`SX_LEAGUE_ALIASES`): varianti con prefisso paese delle
  15 leghe Tier-2 ("England Championship", "Italy Serie B", "Mexico Liga MX",
  "South Korea K League 1"...): un falso DIVIETO su una lega giocabile
  varrebbe piu' di un divieto mancante, perche' azzererebbe il flusso
  autorizzato (stessa ragione del fix 17/09).
- ⚠️ **Come si promuove una lega**: la probation NON e' una promozione, e'
  una misurazione a taglia minima (cap 0.5%). Dopo N chiusure per lega
  (`predictions` + `market_diagnose`) si decide se allinearla al core.

**3) OPZIONE C — LIQUIDITA' −20%** (era 25/5/25/x2.0 dell'11/09):
`SX_MIN_DEPTH_USDC` 25 → **20**, `SX_MIN_LEG_DEPTH_USDC` 5 → **4**,
`SX_MIN_EXEC_DEPTH_USDC` 25 → **20**, `SX_DEPTH_MULTIPLIER` 2.0 → **1.6**
(all'ordine: `richiesto = max(stake × 1.6, 20)`). Default di codice allineati
in `sx_signals.py`, `auto_bet.py`, `multi_market.py`, `liquidity_monitor.py`,
`decision/limits.py`. Gain misurato: **~1 pick/giorno** (la misura dell'11/09
diceva 42/42 eseguibili a 25 USDC: il taglio allarga la fascia dei book
eseguibili, non sblocca un collo di bottiglia). **Protezioni intatte**:
soglia assoluta + multiplo, `inv_sum 0.98-1.08`, fascia quota 1.30-1.80,
favourite gate, `EV_MAX 20%`.

**4) Tripwire aggiornati di proposito** (una soglia allentata senza un test
che la fissi e' un allentamento silenzioso): `test_risk_guards.
TestLiquiditaSx.test_soglie_liquidita_configurate` ora asserisce **uguaglianza
esatta** (20/4/20/1.6) piu' il pavimento `multiplier >= 1.5`;
`test_liquidity_impact` (richiesto 32 per stake 20, vecchie soglie vs attuali);
`test_decision_limits.test_required_depth` **deriva** la formula da `auto_bet`
invece di copiarla; `test_league_gate` nuovi
`test_tutte_le_leghe_tier2_passano`, `test_tier2_edge_alzato_applicato_dal_motore`,
`test_tier2_non_bloccata_per_errore` e l'elenco dei vietati ristretto alle sole
misurate negative; `test_value_filter` +4 test sul tier
(`test_tier2_probation_ammessa_con_parametri_severi`,
`test_leghe_misurate_negative_restano_vietate`, `test_tier_del_core`,
`test_is_sane_tier2_edge_4pp`); `test_league_gate_impact` distingue allowed da
blocked anche per il tier-2 (altrimenti la misura del gate mentirebbe).

**Verifica locale**: 400+ test verdi nei lotti mirati (value_filter,
risk_guards, decision_limits, liquidity_impact/monitor, multi_market,
sx_signals, league_gate(+impact), auto_bet×3, favourites_only, t60_breakers,
market_calib, ou_exclusion, settlement_watchdog, sx_native_settlement,
odds_api, bot, secret_hygiene) + il pacchetto `decision` (adapters, pipeline,
feedback, review, review_telegram, shadow, validation, compare, clv, clv_wiring).
`verify_guardrails.py`: **A-G tutti bloccano** (exit 0). `railway config plan`:
**0 to add, 1 to change, 0 to destroy** (solo il flag non distruttivo di
api-volume).

**⚠️ Da rimisurare fra qualche giorno**: (a) il flusso 1X2 per lega
(tier-2 vs core) sul nuovo ledger; (b) i consumi crediti reali con budget 8
(credit watchdog ogni 6h); (c) le chiusure per lega Tier-2 prima di qualunque
promozione. Il numero di partite analizzate e' il KPI di questa modifica, non
il numero di ordini.

**5) DEPLOY E VERIFICA IN PRODUZIONE (22/09/2026, 00:22 UTC).** Commit
`e68797a` → deployment **`5237a88c` SUCCESS**, health 200. Sul container
(`railway ssh`, sola lettura): `ODDS_DAILY_BUDGET = 8` (letto anche dal codice),
`SETTLEMENT_HEAL_INTERVAL_HOURS = 48` → `tracker._heal_interval_hours() = 48.0`,
`PROBATION_LEAGUES = 15` con `{'min_edge': 0.04, 'kelly_mult': 0.4,
'max_stake': 0.005}`, `league_tier`: Serie B/Liga MX = `probation`,
Serie A/La Liga = `blocked`; liquidita' `20/4/20` + `x1.6` anche in
`multi_market`, `required_depth(1 USDC) = 20`, `required_depth(20) = 32`;
alias `England Championship -> EFL Championship`.
Cicli `auto_bet` puliti (equity 33.55, `0 puntate`, nessun errore).
**Prova diretta del collo di bottiglia**: al momento della verifica le leghe
DOVUTE erano **5** (Serie B, La Liga, Bundesliga, Ligue 1, Eredivisie) —
con il vecchio `budget 2` se ne sarebbero analizzate **2** (e le core
Bundesliga/Ligue 1/Eredivisie sarebbero state rinviate a domani); con budget 8
entrano tutte e 5. Il KPI da guardare domani e' `matches`/`match_analysis`
per giorno (l'obiettivo e' tornare verso le ~100-140 partite/giorno del 19-20/09).

### Verifica flusso + pulizia log (21/09/2026, notte)

**1) VERIFICA DEL FLUSSO IN PRODUZIONE (tutto in sola lettura).**
- **Crediti**: `GET /api/credits` -> **remaining 456**, `days_to_reset` 9 (reset
  01/10), `sustainable_daily` **50.7/giorno**: la chiave nuova e' arrivata e il
  ritmo sostenibile non e' piu' il collo di bottiglia. `estimated_daily_consumption`
  resta `consumption_source: heuristic` (il ritmo MISURATO e' `None`: la cache
  e' stata riscritta con la chiave nuova e servono alcune ore di rotazione).
  **Zero `429`/`403`/traceback** nei log: la crisi crediti e' chiusa.
- **Settlement**: `settlement.open` = **0 bet** e 132 previsioni,
  `overdue_orphans` **0**, `estimated_credits` **0**, `leagues_to_query: []`.
  Motivi del residuo: `league_unmapped` 72, `awaiting_result` 50,
  `not_started` 8, `no_match_row` 2. Il referto gira a costo ZERO e la coda di
  scadenza automatica non ha ritardi.
- **Corsia ordini**: `auto_bet` gira ogni 60s, bankroll `equity 33.55 USDC`
  (disponibile 33.55 + in gioco 0.00), **0 puntate** — e NON e' un blocco:
  `_today_value_picks()` + `_multi_market_picks()` = 0 candidati. Il kill switch
  e' `live`, il feed di mercato e' validato, lo stop-loss e' azzerato.
- **Multi-mercato**: `multi_market_job` ogni 15' -> ingest **95 mercati -> 121
  quote salvate** (6 fixture), 0 segnali giocabili (nessuna linea in fascia
  nelle fixture del momento).
- **Tennis sandbox**: 24 segnali paper, 0 settlement (gira, non tocca il
  bankroll reale).
- **Shadow compare** (`decision_compare`): catena 4 righe (avrebbe giocato 2) |
  corsia 3 puntate | entrambe giocano 1 | bloccate-ma-giocate 1 |
  giocate-ma-saltate 1 | non confrontabili 1. E' il dato della fase di
  confronto: campione ancora minuscolo, nessuna conclusione.
- **Copertura analisi OGGI: 26 partite** (26/09/2026), contro 137 il 20/09 e
  104 il 19/09 — ma e' il numero della rotazione delle **04:00 UTC con
  `ODDS_DAILY_BUDGET=2`**, cioe' PRIMA del deploy del budget 8: le leghe
  analizzate oggi sono tutte fuori whitelist (Serie B, La Liga, Argentina
  Primera, Primera Nacional, LigaPro, Brazil Serie B, Primera A, Liga MX) e le
  35 previsioni di oggi sono **tutte `rejected`**, coerente col gate. Il KPI
  vero (partite/giorno) si legge alla rotazione delle **04:00 UTC del 22/09**,
  che e' la prima con budget 8.

**2) BUG REALE TROVATO DURANTE LA VERIFICA — l'avviso di avvio non veniva MAI
consegnato.** Nei log compariva a ogni deploy
`bot - WARNING - Token o chat_id Telegram mancanti, messaggio non inviato`:
`send_telegram_message_direct` cercava il destinatario SOLO in
`TELEGRAM_CHAT_ID` / `TELEGRAM_CHAT_ID_FALLBACK` (variabili del vecchio
signals-mvp locale). **Su Railway quelle variabili non esistono**, esiste
`ADMIN_CHAT_ID` — quindi il messaggio "Bot avviato in modalita' ..." con stato
circuito, finestra T-60 e bankroll e' stato perso a OGNI deploy.
Fix: fallback su `_admin_chat_ids()` (la funzione che esisteva gia': niente
helper duplicato), con la precedenza alle env locali gia' rispettata. Verificato
sul container: `ADMIN_CHAT_ID: True`, fallback presente nel sorgente, e **0
occorrenze** del warning nei log del deploy nuovo (l'avviso e' partito).

**3) PULIZIA LOG (`a821e04`, deploy `8b7090ae` SUCCESS, health 200).** Misurato
con la coda del log del container: su una finestra di ~2,5 minuti il volume era
dominato da rumore ripetitivo:

| sorgente | righe/finestra | quota |
|---|---|---|
| `apscheduler.executors.default` (avvio/fine di OGNI job) | 104 | 30% |
| `auto_bet` (configurazione + riepiloghi a OGNI ciclo di 60s) | 75 | 22% |
| `apscheduler.scheduler` (registrazione job + "skipped") | 67 | 19% |
| tennis_sandbox / decision.feeds / decision.adapters / bot | 67 | 19% |

Interventi:
- **`secure_logging.setup()`**: **APScheduler a WARNING**. A INFO loggava ogni
  avvio e ogni fine di ogni job (".Running job ..." / "... executed
  successfully") piu' l'elenco "Adding job tentatively" all'avvio: ~45% di
  TUTTO il volume. I **WARNING restano visibili** ("skipped: maximum number of
  running instances" e gli errori di job sono segnali reali, non rumore).
- **`auto_bet`**: "strategia favoriti netti" e "corsie multi-mercato" a DEBUG
  (sono CONFIGURAZIONE identica a ogni giro; restano leggibili con `/autobet`);
  il riepilogo shadow passa a INFO **solo** se c'e' un segnale valutato o un
  comando emesso; "puntata gia' aperta, salto" a DEBUG; e soprattutto **una
  riga di HEARTBEAT per ciclo** (`nessuna puntata (live) — 0 candidati
  giocabili`) cosi' dal log si vede che il job gira senza leggere il blocco di
  configurazione.
- **`decision/feeds.py`** ("riuso dello stato") e **`decision/adapters.py`**
  ("0 segnali aperti su N righe") a DEBUG: sono il funzionamento NORMALE
  ripetuto a ogni giro.
- Verifica POST-DEPLOY sulla stessa finestra: **0 righe APScheduler**, `auto_bet`
  **2 righe per ciclo** (bankroll + heartbeat) e le righe operative FINALMENTE
  visibili (i `rejected` per partita di `sx_signals`, il settlement, gli
  scarti). Riduzione misurata della finestra: ~345 -> ~25 righe.
- **Tripwire nuovi**: `test_secure_logging.test_setup_apscheduler_a_warning`
  (livello + child che ereditano + WARNING ancora visibile),
  `test_bot.TestInvioDirettoTelegram` (4 test: fallback `ADMIN_CHAT_ID`,
  `ADMIN_CHAT_ID` con virgole -> primo, env locali con precedenza, nessun
  destinatario -> non invia) e `test_auto_bet.TestRumoreLog` (2 test: a giro
  vuoto NIENTE configurazione a INFO ma l'heartbeat si', e la configurazione
  resta leggibile a DEBUG).

**4) Verifiche pre-push**: 115 test verdi (`test_auto_bet*`, `test_bot`,
`test_secure_logging`, `test_secret_hygiene`), 122 verdi sul pacchetto
`decision` toccato (`feed`/`adapters`/`shadow`/`clv_wiring`),
`verify_guardrails.py` **A-G tutti bloccano** (exit 0), 0 marker di conflitto,
`compileall` OK. Nessuna env nuova -> nessuna modifica a `.railway/railway.ts`.

**5) Da guardare domani**: il KPI della copertura (rotazione 04:00 UTC con
budget 8) e i Tier-2 in probation da leggere sul ledger per lega con
`market_diagnose.py` prima di qualunque promozione al core.

### Misure leggibili: split del report shadow + lega sul ledger previsioni (22/09/2026)

Direttiva del proprietario, tre azioni in sequenza con via libera totale: il
ROI aggregato per mercato non descriveva nessuna strategia (mescolava cio' che
sarebbe stato giocato con cio' che i gate avevano scartato) e la strategia per
lega era non misurabile perche' il 75,9% delle righe non aveva una lega
attribuibile. Due commit, entrambi deployati e verificati sul container.

**1) SPLIT DEL REPORT SHADOW (commit `70500d5`, deploy #1).**
`multi_market.shadow_report()` espone per mercato quattro bucket — `playable`
(`value`/`strong_value`/`moderate`), `rejected`, `unclassified` (stati ignoti:
mai fatti sparire) e `by_status` per-tier — con `roi`, `profit`, `won/lost/push`,
`other` (verdetti anomali) e **`reliable`**; `format_report()` stampa giocabili e
scartati su RIGHE SEPARATE con l'avviso `⚠️ campione < 30 chiusure: rumore`.
`PLAYABLE_STATUSES` e `MIN_RELIABLE_CLOSED` (30, la stessa soglia di
`league_gate_impact`: il progetto non ha due idee di "campione affidabile").

**2) LEGA SUL LEDGER PREVISIONI (commit `b1f57bd`, deploy #2
`6d127680-9607-40ef-a2fe-8b5b885d5fd1`).**
`predictions` ha la colonna **`league`** (ALTER idempotente in `_get_conn`, quindi
all'avvio; schema anche in `_create_ledger_table`, l'unico punto di definizione).
`save_prediction(..., league=...)` la scrive e in UPDATE usa
`COALESCE(NULLIF(excluded.league,''), predictions.league)`: una rianalisi che NON
passa la lega **non cancella** il valore registrato (i percorsi sono piu' d'uno:
`fixture_engine` cand["league"] o la variabile di funzione, `sx_signals`
`league_name`, `multi_market` `fixture["league"]`). `get_predictions()` espone la
lega; `predictions_summary(..., statuses=)` filtra per stato (default None =
comportamento storico invariato, nessun chiamante cambiato).

**3) DIAGNOSI PER MERCATO SOLO SUI GIOCABILI (stesso commit).**
`market_diagnose` basa `totals`, `markets`, `critici` e `azioni`
**esclusivamente** su `value_filter.PLAYABLE_TIERS`; le righe escluse finiscono
in un blocco `excluded` **dichiarato e mai usato per giudicare** (ricavato per
SOTTRAZIONE, cosi' uno stato nuovo non puo' sparire dai conti). La tripla dei
tier ha ora UNA definizione sola (`value_filter.PLAYABLE_TIERS`, con
`multi_market.PLAYABLE_STATUSES` come ALIAS) e tre tripwire la difendono. Nuovo
flag `--all-statuses` = confronto col comportamento pre-22/09, con banner
"CONFRONTO, non decisionale" (anche l'etichetta del campione cambia: chiamarlo
"giocabile" sarebbe una bugia).

**4) I NUMERI REALI (container, 22/09, sola lettura `mode=ro`).**
```
Campione giocabile: 119 chiusi (43V/70P/6push) | ROI -8.83% | EV +11.01 | gap -19.84
Fuori dai calcoli:  529 righe non giocabili | ROI -7.28 -> costo dei gate
1X2  85 chiusi | ROI -21.64% | gap -32.38pp | hit 29.8% vs prob 43.6%  << critico
OU   30 chiusi | ROI +21.21% | gap +10.10pp
AH    4 chiusi | ROI +37.97% | gap +22.02pp  (campione minimo)
per tier: 1X2 strong_value n=35 ROI -38.43% | 1X2 value n=50 -9.88%
          OU value n=10 +55.70% | OU strong n=20 +3.97% | AH strong n=4 +37.97%
```
**Il -21,64% del 1X2 e' il P/L VERO delle righe marcate "giocabili"** (prima
l'aggregato lo mascherava a -10,35% mescolandolo coi 529 `rejected`): il gap di
-32,4pp con un'overconfidence di -13,8pp dice che l'EV atteso del modello sul
1X2 e' gonfiato, non che i gate taglino troppo. I numeri OU/AH del nuovo report
**riproducono esattamente** la replica SQL con cui erano stati misurati
(+21,21% / +37,97%): il contatore nuovo e' coerente con la misura manuale.

⚠️ **MA lo split per stato NON basta a giudicare la strategia CORRENTE — e il
dato lo dimostra.** `status` registra il tier con le soglie DEL MOMENTO in cui
la riga e' nata, non con quelle di oggi: del campione 1X2 giocabile, **74/85
righe sono PRE-11/09** (quota media **2,87**, cioe' fuori dalla fascia
1.30-1.80 attuale: oggi sarebbero `rejected`) e solo **11/85 sono dal 11/09**
(quota media 1,94, ROI -17,36%). Quindi il -21,64% e' in gran parte la storia
di una strategia RITIRATA: per giudicare l'attuale serve un filtro in piu'
(fascia quota corrente o `--since`) — e' la prossima rifinitura naturale, non
un difetto dello split, che era comunque il prerequisito (senza di esso il
numero era inutilizzabile a prescindere).

**5) PERCHE' LA COLONNA ERA NECESSARIA (misurato).** Attribuzione delle 648
righe chiuse: **156 (24,1%)** via colonna O join; le altre 504 no. Causa: le
righe `matches` vengono POTATE (`clear_old_matches`), quindi la lega evapora con
la partita — non e' un problema di campione. Conseguenza: sui **giocabili
chiusi** solo 9/119 hanno una lega (Eredivisie 6, Ligue 1 2, PL 1) e **110 sono
`None`** → la strategia per lega (e quindi la promozione dei Tier-2) resta NON
misurabile sullo storico; lo diventa **da adesso** per le righe nuove (prime 5
righe `multi_market` gia' con lega, verificate). ⚠️ Un backfill delle righe
ancora joinabili (156) NON cambierebbe nessun numero oggi: servirebbe solo a
"congelare" la lega prima che la potatura cancelli il match. Non eseguito (non
richiesto): e' un `UPDATE` di una riga sola, da decidere.

**6) INTEGRITA' POST-MIGRAZIONE (container).** `PRAGMA quick_check` = **ok**,
`PRAGMA integrity_check` = **ok**, `predictions` = **782 righe** (invariate: la
migrazione e' additiva), 15 colonne, `league` presente, 5 righe gia' popolate
dal job `multi_market` (ogni 15'). Il vecchio schema resta leggibile
(`COALESCE(p.league, m.league)` copre entrambi i casi).

**7) TRIPWIRE.** `test_predictions.py` (+5: salvataggio della lega, la lega
sopravvive a una chiamata senza lega e alla lega vuota, lega assente = None,
**migrazione su DB vecchio** senza perdita di righe e idempotente, filtro
`statuses` case-insensitive e con tupla vuota ≠ nessun filtro);
`test_market_diagnose.py` (+7: gli scartati non entrano nei giudizi, 90
giocabili + 500 scartati NON fanno un campione maturo, `excluded` malformato non
solleva, il report dichiara gli esclusi, integrazione `analyze_db` con entrambe
le popolazioni + controprova `--all-statuses`, nessuna copia della tripla);
`test_multi_market.py` (alias, non copia). Regressioni verdi: 277 test (auto_bet
x3, bot, web_api, settlement x4, sx_signals, reports, performance_report,
ml_dataset, dedup_ml, backup, liquidity_monitor, t60_breakers, report_audit) +
324 (decision: adapters/compare/shadow/clv/feedback + value_filter, risk_guards,
favourites_only, league_gate, ou_exclusion, tier, market_calib, poisson_engine);
`verify_guardrails.py` **A-G tutti bloccano**; 0 marker di conflitto,
`compileall` OK. Nessuna env nuova.

### Direttiva 22/09/2026: strategia 1X2 CONGELATA in attesa del campione

**Direttiva del proprietario, dopo la correzione d'era del campione 1X2**: il
sistema e' considerato pronto e pulito — **non si tocca piu' nulla** finche' il
nuovo setup non ha prodotto abbastanza chiusure REALI.

**1) 1X2 RESTA ATTIVO, SOGLIE E BLEND CONGELATI.** Nessuna modifica a
`MARKET_EDGE_MIN`/`MARKET_EDGE_MODERATE` (0.02), `MARKET_EDGE_STRONG` (0.04),
`EV_MIN` (0.02) ne' ai pesi del blend dinamico
(`market_calib.blend_probability` / `LEAGUE_EFFICIENCY`). ⚠️ In particolare
**NON** leggere il **-21,64%** del 1X2 giocabile come prova contro la strategia
corrente: **74/85 righe sono PRE-11/09** (quota media 2,87, fuori dalla fascia
1.30-1.80 attuale) — e' la storia di una strategia RITIRATA (vedi la sezione
"Misure leggibili" del 22/09).

**2) GATE DI VALUTAZIONE: 30-40 CHIUSURE REALI DELL'ERA NUOVA.** Nessun giudizio
su blend/EV/soglie prima. Campione al 22/09: **11 chiusure** (ROI -17,36%,
quota media 1,94) -> si continua a raccogliere. Come si contera' quando sara' il
momento: righe `predictions` con `status` in `value_filter.PLAYABLE_TIERS`,
`esito_finale IS NOT NULL`, nate/chiuse **dall'11/09 in poi** e in fascia quota
1.30-1.80. La colonna `league` (22/09) rende tracciabili per lega le righe
nuove (sullo storico vecchio la lega NON e' recuperabile in modo utile).

**3) NESSUN BACKFILL — il passato e' SUNK COST.** Le 156 righe storiche ancora
joinabili restano come sono: **nessun `UPDATE` di lega**, nessuna ricostruzione
dalle vecchie competizioni. Si misura SOLO sul ledger nuovo, perfettamente
tracciato (l'era vecchia non e' una base di confronto valida).

**4) MONITORAGGIO PASSIVO — nessuna modifica di codice, nessuno strumento
nuovo.** La telemetria esistente copre gia' tutto: credit watchdog (6h,
`credit_budget_status`), settlement watchdog (4h), drift watchdog (6h),
`decision_compare_job` (6h, confronto catena/corsia), liquidity monitor (6h).
Il bot riprende da solo a operare (kill switch `live` + provider pronto,
settlement attivo, stop-loss fantasma rimosso, crediti sani): **si attende la
fine della pausa nazionali senza interventi manuali**.

**5) RIFINITURA ANNOTATA E NON ESEGUITA**: un filtro d'era su `market_diagnose`
(fascia quota corrente o `--since`) e' l'unico pezzo che serve per leggere il
campione nuovo senza filtri a mano — si implementa quando il campione raggiunge
la soglia, non prima.

### Diagnosi 24/09/2026: flusso scommesse fermo — depth/edge FALSIFICATI, causa = gate leghe + rotazione dormiente

**Segnalazione del proprietario**: "il bot ha completamente interrotto la
produzione di scommesse". Diagnosi eseguita in **sola lettura** sul container
(`railway ssh`, SQLite `mode=ro`, zero ordini, zero crediti). Due delle tre
ipotesi proposte sono state **falsificate dai dati**; la causa e' un'altra.

**1) Depth gate (20/4/20 x1.6) — NON e' la causa.** Il registro scarti
(`data/execution/liquidity_skips.jsonl`, 61 eventi totali) ha **1 solo** scarto
per profondita' dal 12/09, e oggi 1 match su ~25 (`depth_esito_2`: leg 3.65 <
4.0, Nigeria–Madagascar). Le env restano ai default del 21/09: nessuna soglia
toccata. Un collo di bottiglia di liquidita' avrebbe riempito il registro.

**2) min_edge probation 4% — NON e' la causa.** Dal 11/09 nelle 20 leghe
ammesse c'e' **UNA sola** prediction (MLS, 23/09) e il suo rifiuto e' per **EV**
(-12.4%), non per edge. In 4 giorni un solo scarto "EV troppo basso". Il 4%
non ha mai avuto modo di agire: **non arrivano candidati**.

**3) Attribuzione degli scarti del 24/09** (`value_filter.is_sane` di
produzione applicata alle 78 righe del ledger):

| motivo | righe |
|---|---|
| `lega 'Africa Cup of Nations' esclusa` | 44 |
| `lega 'UEFA Nations League' esclusa` | 31 |
| `lega 'Major League Soccer' esclusa` | 2 |
| `lega 'Primera A' esclusa` | 1 |
| depth / edge / favourite gate | **0** |

**78/78 scartate dalla LEGA**. Il motivo contingente e' la **finestra delle
nazionali**: nei 7 giorni successivi 45 delle 80 partite in cache sono UEFA
Nations League e i campionati di club ammessi sono in pausa; nelle **prossime
48h** le partite in leghe ammesse erano **2** (Liga MX).

**4) DUE DIFETTI REALI TROVATI (non ipotesi, misurati).**
- **Leghe ammesse DORMIENTI**: 10 leghe ammesse erano a **30gg** di rotazione
  (`Turkey Super Lig`, `Allsvenskan`, `Argentina Primera`, `Austrian
  Bundesliga`, `Eliteserien`, `J1 League`, `K League 1`, `Scottish
  Premiership`, `Superliga Danimarca`, `Swiss Super League`): con partite in
  calendario **non venivano mai interrogate**, quindi non potevano produrre
  alcun candidato qualunque fosse la soglia. E' la configurazione di emergenza
  crediti del 05/09, mai ripristinata. Dal 11/09: 1 prediction su 688 righe
  nelle leghe ammesse.
- **Falso divieto su lega ammessa**: `multi_market` salvava l'etichetta GREZZA
  di SX (`Major League Soccer`) invece della chiave della strategia (`MLS`) →
  il gate la leggeva come lega VIETATA e scartava candidati con **EV +52%** ed
  **edge +9.5pp** con "lega esclusa per ROI negativo", che per quella lega in
  probation non e' vero. Il resolver esisteva ed era corretto: **quel percorso
  non lo usava** (il tripwire del 17/09 testava il resolver, non il chiamante).

**5) Nessun blocco tecnico**: kill switch `live` + `provider_ready: true`,
settlement attivo, stop-loss giornaliero **solo riferimento** (`stopped_until:
null`, `basis_key: live_equity`), CB2 T-60 non armato, crediti **416**, feed di
mercato SX validato (63–74/74 quote conformi), wallet 33.55 USDC. Il sistema
gira: era **progettato per non produrre segnali** in questa finestra.

**6) DECISIONE DEL PROPRIETARIO (via `ask_user`)**: (a) **nessun allentamento**
di soglie/gate — fix dei difetti e rotazione; (b) **riattivare a 7gg** le 10
leghe dormienti.

**7) INTERVENTI APPLICATI.**
- `value_filter.LEAGUE_ALIASES` + `canonical_league()`: **difesa in profondita'
  del gate** — il nome della lega viene normalizzato in `league_allowed`,
  `league_tier`, `get_league_strategy`, quindi nessun percorso dipende da come
  una fonte scrive il nome. SOLO alias univoci (`Major League Soccer`/`USA MLS`
  → `MLS`); `Brazil Serie B` (Serie B brasiliana) **NON** diventa `Serie B` e
  resta vietata.
- `multi_market._resolve_league_label()` usata in `discover`: la risoluzione
  sta **alla fonte** (riusa `sx_signals._league_sx_to_sports_map`, import
  pigro, con `canonical_league` come ripiego).
- `odds_api.SPORTS_INTERVAL_DAYS`: le 10 leghe ammesse da 30 → **7gg** (tutte
  le leghe giocabili sono ora interrogate almeno ogni 7 giorni). Costo mensile
  della rotazione **157.7 → 190.6** su un tetto di 460 (`ODDS_DAILY_BUDGET=8`).

**8) IMPATTO MISURATO — onesta: il fix dei nomi e' CORRETTEZZA, non volume.**
Sulle 709 righe dal 11/09: 89 erano bloccate da un nome lega non canonico, ma
le righe con lega `Major League Soccer` avevano quota **fuori fascia** (6.1,
9.3) e le 27 righe che con l'alias passano `is_sane` hanno lega `None`
(pre-migrazione 22/09) e restano **fail-closed** nella corsia ordini (lega
assente = non si ordina dal 17/09). Quindi: **zero pick sbloccati oggi**; la
leva reale e' la **rotazione**, e il flusso riparte quando i campionati ammessi
tornano in calendario (~28-30/09) o subito via multi-mercato sulle leghe
riattivate.

**9) TRIPWIRE NUOVI** (una correzione senza test e' una correzione che si
perde): `test_odds_api.test_leghe_ammesse_mai_dormienti` (ogni lega ammessa
DEVE avere intervallo ≤ 7gg — il difetto del 24/09; `test_rotazione_crediti`
aggiornato: Turkey 30→7, con una lega di nazionali come esempio di dormiente);
`test_league_gate.TestNomiDelleLegheAmmesse.test_gate_ammette_il_nome_grezzo_del_provider`
+ `test_alias_non_fonde_leghe_diverse`; `test_multi_market.TestTripwire.
test_etichetta_lega_risolta_in_discovery` + tripwire sul sorgente
(`"league_label": _resolve_league_label(`).

**10) VERIFICHE**: ~718 test verdi nei lotti mirati (`odds_api`, `league_gate`,
`multi_market`, `value_filter`, `sx_signals`, `auto_bet` x3, `favourites_only`,
`risk_guards`, `t60_breakers`, `league_gate_impact`, `ou_exclusion`, `tier`,
`market_calib`, `liquidity_monitor/impact`, `decision` x6, `predictions`,
`bot`, `web_api`, `settlement_watchdog`, `reports`, `secret_hygiene`),
`verify_guardrails.py` **A-G tutti bloccano**, 0 marker di conflitto,
`compileall` OK.

**11) LEZIONE PERMANENTE**: un gate di strategia che confronta NOMI dipende da
come ogni fonte scrive il nome — e una lega ammessa a 30gg di rotazione e'
**dormiente di fatto**: le due cose insieme possono azzerare il flusso senza
che nessuna soglia di rischio sia cambiata. Quando il flusso si ferma, il primo
controllo non e' la soglia: e' **quante partite in leghe ammesse sono state
interrogate**.

### Misura del flusso a 24 ore: `flow_measure.py` (24/09/2026)

Richiesta del proprietario: *"misurare il flusso a 24 ore"*, dopo la diagnosi
"flusso scommesse fermo". Scelta esplicita (via `ask_user`): **strumento
riutilizzabile + misura reale**, non una query a mano — la diagnosi del 24/09
era stata fatta con un'attribuzione manuale, e una misura manuale non si
ripete.

**1) IL MODULO** (`flow_measure.py`, diagnostica top-level come
`liquidity_impact.py`/`league_gate_impact.py`). Misura il FUNNEL, stadio per
stadio, su una finestra (default **24h**):

| stadio | fonte | cosa dice |
|---|---|---|
| 1. ANALISI | `match_analysis` + `matches` | partite analizzate e leghe per stato (core/probation/blocked/**unknown**) |
| 2. SEGNALI | `predictions` | righe per mercato e stato, giocabili (`PLAYABLE_TIERS`), aperte |
| 3. GATE | ricalcolo | **attribuzione di OGNI scartata al motivo** del gate |
| 4. ORDINI | `bets` | righe per modalita' (live/sim/rejected-t60), stake |
| 5. CATENA | `decisions` | verdetti e stati della catena `decision/` (shadow) |
| 6. LIQUIDITA' | log JSONL | scarti del monitor SX (edge perso) |

Perche' e' affidabile: il motivo di scarto si ricalcola con il gate di
PRODUZIONE (`value_filter.is_sane`, con `favourites_only` come in
`multi_market` per OU/AH) e la tripla dei tier si importa da
`value_filter.PLAYABLE_TIERS` — mai una copia delle soglie, che cambierebbe
senza che la misura se ne accorga. Sola LETTURA (`mode=ro`), offline (nessuna
rete, zero crediti, zero ordini), fail-safe (DB assente/corrotto/tabella
mancante -> `error` dichiarato o zeri, mai un'eccezione).

CLI: `venv/bin/python flow_measure.py [--hours N] [--json] [--db PATH]`
(`FLOW_WINDOW_HOURS` cambia il default). Sul container:
`railway ssh --service api -- bash -lc "PYTHONPATH=/app python3 /tmp/flow_measure.py --hours 24"`.
`test_flow_measure.py` = **28 verdi, tutti OFFLINE** (ledger SQLite temporaneo
con lo schema di produzione, date SEMPRE relative a `now`): tripwire di sola
lettura (un `UPDATE` deve essere RIFIUTATO), formati di data reali (`T`/`Z`/
offset/spazio), finestra, attribuzione per motivo, fail-safe, verdetto.

**2) BUG TROVATO DALLA MISURA STESSA (corretto).** `league_tier("")` risponde
`blocked`, quindi la prima versione contava “lega IGNOTA” come “lega
VIETATA”: sul ledger vero stampava “13 giocabili, 0 in leghe ammesse”, che si
legge come un gate che bypassa la strategia. Falso allarme: quelle righe sono
del 19-20/09 e hanno perso la lega perche' la riga `matches` e' stata POTATA
(`clear_old_matches`) e la colonna `predictions.league` nasce il 22/09. Nuovo
`_league_state()` con il bucket **`unknown`** (stessa ragione del bucket
`UNKNOWN` di `league_gate_impact`) + riga di caveat nel report:
**“lega ignota” non e' un divieto e non e' giudicabile dal gate**. I 2 ordini
del 20/09 (bet #43 AH, #44 1X2) NON sono una prova di bypass: allora la riga
`matches` esisteva e la lega era nota.

**3) MISURA REALE IN PRODUZIONE (24/09/2026, 19:35 UTC, sola lettura).**
Modulo copiato in `/tmp` e rimosso a fine misura (`PYTHONPATH=/app`): il file
NON e' deployato (e' uncommitted), quindi la misura non ha richiesto un push.

* **24h**: 40 partite analizzate (blocked 33, probation 7) | **114 segnali**
  (1X2 21, OU 52, AH 41) | **0 giocabili** | 112 scartati per **league_blocked**
  + 2 `odds_max` | leghe scartate: Africa Cup of Nations 61, UEFA Nations
  League 50, Major League Soccer 2, Primera A 1 | **0 ordini**, 0 decisioni,
  1 scarto liquidita'.
  -> Conferma la diagnosi: la finestra delle **nazionali** (AFCON + Nations
  League = 111 delle 114 righe) e' l'unico motivo di stop; **nessuno scarto per
  edge/EV/fascia quota**. Le leghe ammesse non sono dormienti (Brasileirao 6,
  MLS 1 analizzate): la rotazione a 7gg del 24/09 sta funzionando.
* **7 giorni**: 346 partite analizzate (**unknown 300**) | 526 segnali | 13
  giocabili (**tutti con lega non attribuibile**) | 512 scartati (`odds_max`
  272, `league_blocked` 134, `ev_low` 86, altri) | **2 ordini live** (20/09,
  stake 2.0) | 2 decisioni `approve` | 11 scarti liquidita'. **387 righe senza
  lega attribuibile**: su quelle il gate non e' giudicabile (caveat dichiarato).

**4) LA LEZIONE**: il gate leghe e' il collo di bottiglia, ma la sua misura va
letta con il periodo giusto — nelle 24h il 97% degli scarti e' una **finestra
contingente** (nazionali), non una soglia; e i numeri sulle righe con lega
persa dal pruning **non provano nulla** su oggi. Il KPI da guardare quando i
campionati di club tornano in calendario e' la riga 2, non il conteggio degli
scarti.

### Fase 1 — Diagnostica "downtime" (25/09/2026): NESSUN guasto, il fermo e' operativo

Richiesta del proprietario: capire la causa del downtime (log hosting, API
del bookmaker, integrita' DB). Esito: **non c'e' downtime tecnico**. Il
servizio e' online, il bot gira, e **nessuno dei tre sospetti era la causa**.

**1) LOG HOSTING — nessun crash, nessun riavvio, nessun loop.** Deploy attivo
`8c9b0d1` (24/09 18:10 UTC) SUCCESS; PID 1 `python run_all.py` vivo da
**6.36 h** (nessun "Bot avviato"/"Adding job" nelle ultime 2h); ultime 500
righe: **0 ERROR, 0 Traceback**. Unica WARNING ricorrente: leghe non mappate a
SPORTS_MAP (LigaPro, Primera A) → insaldabili per scelta, costo 0 crediti.

**2) API BOOKMAKER/EXCHANGE — tutte valide.** Nota: **Betfair e' stato rimosso
dal progetto il 06/09** e **Pinnacle non e' un'API integrata** (e' lo sharp di
riferimento letto via the-odds-api). Gli attori reali sono tre e sono sani:
SX Bet (provider `sxbet`, `dry_run: false`, **33.5535 USDC** disponibili,
escrow 0 → credenziali valide), the-odds-api (**412 crediti**, reset 01/10,
consumo misurato 11.4/g vs 82.4/g sostenibili, **0 × 429/403**), Telegram
(`getMe` **HTTP 200** su @Calcifrrbot → token valido, nessun 409 conflict).

**3) INTEGRITA' DB — pulita.** `/app/data/quotaverace.db` (6.28 MB):
**`quick_check` = ok, `integrity_check` = ok**, `journal_mode = delete`,
**nessun `-wal`/`-journal` pendente** → nessuna transazione interrotta, nessun
lock. Migrazioni tutte applicate. Disco volume 250 MB liberi (42% usato).

**4) LA CAUSA VERA: 5 giorni senza ordini** (ultima bet #44 del 20/09 12:00;
ultima riga della catena `decisions` 20/09 12:25). Le 207 predictions aperte
sono **tutte `rejected`**: zero candidati giocabili. Rifiuti delle ultime 2h:
Africa Cup of Nations 86, UEFA Nations League 64, Brasileiro Serie B 13,
Primera A 11 — motivo sempre **EV negativo** (da -2.3% a -27.8%), mai edge,
fascia quota o liquidita'. Le 9 `strong_value` "senza lega" degli ultimi 6
giorni sono tutte **chiuse del 19-20/09**: non sono un collo di bottiglia
attivo. Nessun blocco: kill switch `live`, provider pronto, settlement attivo,
stop-loss solo riferimento, CB2 non armato.

**5) ANOMALIA DI CONFIGURAZIONE**: `DECISION_SHADOW_PERSIST = '1'` e'
**impostata su Railway** (la memoria la registrava come default OFF). Oggi non
fa nulla (0 segnali giocabili → 0 scritture, `decisions` fermo a 4 righe), ma
quando il flusso ripartira' la shadow mode inizio' a scrivere su `decisions`.

### Fase 2 — Sbloccare il flusso: la rotazione non catturava le odds (25/09/2026)

Direttiva del proprietario su quattro voci di "scalabilita'": dopo la
verifica, **tre erano gia' implementate** (value betting = il core; Kelly
dinamico in 4 moduli; ML+dropping odds attivi) e le scelte prese sono state
**flusso prima**, **Kelly parametrizzabile**, **niente multisport**, **niente
xG** (prima il campione).

**1) LA MISURA CHE HA TROVATO IL VERO COLLO DI BOTTIGLIA.** Le cache quote
delle leghe core erano state riscritte 2,5 giorni fa con **`payload: []`** e
`remaining: 452`: la chiamata RIUSCIVA e restituiva zero partite. Test diretto
sull'API reale (finestra `now` → `now`+7gg):

| lega | eventi | note |
|---|---|---|
| MLS | **15** | kickoff 26/09 |
| Liga MX | **9** | kickoff 26/09 |
| UEFA Nations League | **38** | kickoff 25/09 |
| Serie A / Bundesliga / La Liga / Eredivisie | **0** | 7 giorni di distanza |
| Brasileirao / Argentina Primera / AFCON | **0** | idem |

Le leghe sono tutte `active=True` su `/v4/sports`: non e' copertura. La
spiegazione e' il **tempo di pubblicazione**: the-odds-api pubblica le odds con
**1-3 giorni** di anticipo. E due scoperte operative:
- **le chiamate VUOTE non addebitano credito** (4 chiamate vuote → `remaining`
  invariato a 412; -1 su ognuna delle 3 con dati): il costo lo fanno le leghe
  che HANNO partite, non il numero di interrogazioni;
- conseguenza del profilo del 24/09 (**finestra 7gg E rotazione 7gg**): una lega
  interrogata il giorno X non vedeva **mai** le partite del weekend X+4 (odds
  pubblicate a X+2) e alla successiva interrogazione (X+7) erano passate →
  **zero candidati per sempre**, qualunque soglia di edge/EV. Da qui il crollo
  delle analisi: 137 il 20/09 → 24 il 25/09.

**2) FIX — le 20 leghe AMMESSE a 2 GIORNI (`odds_api.SPORTS_INTERVAL_DAYS`).**
MISURA del tetto crediti (invariante `cost = Σ(30/intervallo) <= 460`):
attuale **190.6**; con le ammesse a 2gg **370.6** ✓; a 1gg **670.6** ✗. Quindi
**2 giorni e' il massimo sostenibile**. Serie A e La Liga (non ammesse) restano
a 3gg, coppe 7gg, resto 30gg.
**Env:** `ODDS_DAILY_BUDGET` da 8 a **16** (20 leghe a 2gg ≈ 10 dovute/giorno:
con 8 meta' verrebbero rinviate; l'env e' gia' in `preserve()`).

**3) TRIPWIRE.** `test_rotazione_crediti` (EPL e Turchia 3/7gg → **2gg**),
`test_leghe_ammesse_mai_dormienti` ora asserisce **uguaglianza a 2gg** (un
allargamento a 1gg, che sfonda il tetto, deve rompere il test),
`test_stagger_spalma_le_leghe_core` verifica che le 20 ammesse coprano
ENTRAMBE le fasi (0 e 1) — se cadessero tutte nello stesso giorno meta' dei
giorni sarebbe a zero analisi. Due test dipendevano dall'ordinamento per
intervallo: `test_budget_giornaliero_cap` ora verifica che la lega scelta abbia
l'intervallo MINIMO (non piu' il nome "Serie A", che non e' un contratto) e
`test_fetch_analizza_anche_squadre_sconosciute` alza il budget nel test (con
cache vuota tutte le leghe sono dovute e il tetto tagliava fuori la lega
esercitata).

**4) KELLY PARAMETRIZZABILE.** `KELLY_MIN/MAX_FRACTION` erano **gia'** da env;
ora lo sono anche **`KELLY_BASE_FRACTION`** (0.25, usato nel CALCOLO alla riga
90 e nei messaggi), **`DRAWDOWN_THRESHOLD`** (0.10) e **`DRAWDOWN_REDUCTION`**
(0.50). Nuovo `_ratio_env()`: clamp in [0, 1] con fallback al default e
**warning** per valori impossibili (mai in silenzio), piu' allineamento
`MAX >= MIN`. ⚠️ **Il floor dell'exchange (1 USDC) prevale su qualunque
frazione finche' il bankroll resta sotto ~50-100 USDC** (soglie misurate il
17/09: il cap morde da 50.25 USDC per strong_value e 100.50 per value):
parametrizzare il Kelly oggi NON cambia una singola puntata con 33.55 USDC.
**IaC**: aggiunte in `preserve()` `KELLY_BASE_FRACTION`, `DRAWDOWN_THRESHOLD`,
`DRAWDOWN_REDUCTION`, `STAKE_MIN_EUR`, `STAKE_STEP_EUR`, `BET_STAKE_EUR`.
⚠️ Trovato un **nome divergente**: `auto_bet` legge `MIN_STAKE_EUR` (floor
1.0) mentre `adaptive_staking` legge `STAKE_MIN_EUR` (0.01) — IaC ne
dichiarava solo il primo, quindi il secondo sarebbe stato distrutto da
`railway config apply`. Ora entrambi sono dichiarati.

**5) VERIFICHE**: **673 test verdi** nei lotti mirati (`test_odds_api`,
`test_adaptive_staking`, `test_decision_limits` 86 · `test_flow_measure`,
`test_liquidity_monitor`, `test_value_filter`, `test_league_gate` 130 ·
`test_auto_bet`×3, `test_favourites_only`, `test_risk_guards`,
`test_t60_breakers`, `test_league_gate_impact`, `test_market_calib`,
`test_tier` 251 · `test_bot`, `test_settlement_*`, `test_scores_parsing`,
`test_secret_hygiene`, `test_decision_*` 206);
`railway config plan` = **0 to add, 1 to change, 0 to destroy**; 0 marker di
conflitto nel progetto; `compileall` OK.

### Esito del deploy (25/09/2026, 01:11 UTC) — board sbloccato, 0 value

Commit `5c7da59` → deploy **`fce20e3f` SUCCESS**. Verificato sul container:
`ODDS_DAILY_BUDGET=24`, `EPL/Turkey/MLS = 2gg`, costo mensile 370.6/460,
Kelly 0.05/0.05 (base 0.25), wallet 33.55 USDC, kill switch `live`.

**Giro di analisi forzato** (la stessa funzione del `morning_job` delle 04:00
UTC — i job di analisi girano 3 volte al giorno, NON ogni ciclo di polling:
`matches`/`match_analysis` si popolano solo li'): **22 leghe interrogate**, di
cui 3 con partite — `soccer_korea_kleague1` 1, `soccer_usa_mls` **15**,
`soccer_mexico_ligamx` **9**. Costo **3 crediti** (406 → 403): le chiamate
vuote non addebitano.

| metrica | PRIMA (01:12) | DOPO (01:22) |
|---|---|---|
| partite in leghe AMMESSE | 2 | **27** |
| `matches` totali | 55 | 69 |
| MLS / Liga MX / K League 1 | 1 / 1 / 0 | **16 / 10 / 1** |
| `match_analysis` | 1010 | **1035** |
| analisi di oggi | 25 | 50 |
| rinvii dal budget | 14 (stimati) | **0** |
| segnali value | 0 | **0** |

**Il budget 24 era indispensabile — non prudenza, necessita'**: nell'ordine
effettivo del giro (le 20 ammesse sono a 2gg, quindi passano PRIMA delle 3gg)
**MLS e' la 16ª lega e Liga MX la 18ª**. Con `ODDS_DAILY_BUDGET=8` (o con il 16
inizialmente proposto) entrambe sarebbero state rinviate al giorno dopo e le
partite di Liga MX (kickoff 26/09) perse **di nuovo**, esattamente come prima
del fix. Il budget 24 ha salvato le due leghe che avevano le partite.

**0 segnali value, e va bene cosi'** (direttiva del proprietario): i 25 match
sono tutti `rejected` con EV da -3.5% a -28%, le 207 predictions aperte restano
tutte `rejected`, e con `FAVOURITES_ONLY` un match senza esito qualificato non
scrive nulla nel ledger. Il gate EV fa il suo mestiere: scartare EV negativi su
un bankroll di 33.55 USDC e' protezione della cassa, non un difetto. **Il
congelamento del 22/09 resta valido** (nessuna modifica a gate/modello).

Effetto collaterale noto: `clear_old_matches()` ha rimosso dal board
Brasileirao, Argentina Primera e LigaPro (partite passate o leghe non
interrogate nel giro). Il board contiene solo la finestra corrente: non e' una
perdita di dati (restano in `match_analysis` e nel ledger).

**Cosa guardare al ritorno dei campionati Tier-1 (~29-30/09):** che il giro di
analisi trovi partite nelle leghe CORE e che `matches`/`match_analysis`
risalgano verso le ~100-140/giorno del 19-20/09. Watchdog attivi e verificati
nei log: `credit_watchdog`, `settlement`, `drift`, `liquidity_monitor`,
`decision_compare`, `backup`. Il prossimo giro automatico e' alle 04:00 UTC.

### Pivot Top-Down (Steam Chasing): Pinnacle come oracolo — FASE 1 PROBE (25/09/2026)

Direttiva del proprietario: **congelare il modello bottom-up (Poisson)** e
decidere guardando il MERCATO: Pinnacle come fonte della verita', SX Bet come
prezzo da confrontare, il ritardo fra i due come unico edge. Nuovo modulo
`pinnacle_oracle.py` + probe `test_pinnacle_api.py` (branch
`feature/top-down-pinnacle`). **FASE 1 = probe: nessun ordine, nessuna
scrittura sul ledger, nessun collegamento alla pipeline.**

**1) TRE COSE MISURATE PRIMA DI SCRIVERE CODICE (non assunte).**
- **Pinnacle e' GIA' nel payload che paghiamo**: la fetch quote usa
  `regions=eu` SENZA filtro `bookmakers`. Verificato sulle cache di
  produzione: **9 leghe su 9** hanno Pinnacle. Aggiungere
  `bookmakers=pinnacle` non compra dati nuovi — riduce il payload. Il costo
  marginale dell'oracolo e' quindi **ZERO** se si estrae dalla fetch che
  facciamo gia'.
- **Il de-vig esiste gia'**: `market_calib.devig` con `multiplicative` /
  `power` / `shin` + `market_implied`. Nessuna formula duplicata: `power`
  (default del progetto) corregge il favourite-longshot bias — alza la
  probabilita' del favorito **rispetto al proporzionale** e abbassa il
  longshot (misurato: 1.75/3.60/4.50 -> p1 0.5483 con power, 0.5333 con
  multiplicative).
- **La cadenza e' il vincolo, non l'estrazione** (vedi punto 3).

**2) PROBE ESEGUITO SUI DATI REALI (container, 25/09, `/tmp` isolato e poi
rimosso: la produzione non e' stata toccata).**
- `--from-cache` (**0 crediti**): 9 leghe, **65 partite, 62 con un 1X2
  Pinnacle COMPLETO (95,4%)**, overround misurato **3,8-5,4%** (MLS 15/15,
  League Two 12/12, Liga MX 9/9, Bundesliga 2 9/9, League One 7/7, Primeira
  Liga 7/10, K League 1 1/1, Brazil B 1/1, Superettan 1/1).
  → L'oracolo e' **disponibile e gratis** sulle partite che gia' analizziamo.
- `--live soccer_usa_mls` (**1 credito**): status 200, 16 eventi, **15 con 1X2
  Pinnacle completo**, `x-requests-last = 1`, crediti **402**.
- `--live soccer_italy_serie_a` (lega **senza partite** in finestra): status
  200, **0 eventi**, `x-requests-last = 0`, crediti **402** (invariati).
  → **Le chiamate vuote NON addebitano**: il costo dipende dalle leghe che
  HANNO partite, non dal numero di interrogazioni. E' il numero che rende
  sostenibile (o no) un job di confronto.

**3) VERDETTO DI SOSTENIBILITA' (conti con i numeri sopra).** Un job di
confronto "in tempo reale" **ogni 5 minuti e' insostenibile**: 1 lega ogni 5'
= 288 chiamate/giorno contro un budget di ~500/mese (~16/giorno). Il piano
free impone la forma della pipeline:
- **percorso primario = cache** (`--from-cache`, 0 crediti): l'oracolo si
extrae dal payload che la rotazione analisi scarica gia';
- **percorso live = diagnostica**, budgettizzato: il costo e' 1 credito per
  lega-con-partite. Con le ~3 leghe/giorno che hanno partite (misura del
  25/09), un controllo a T-60 costerebbe ~3 crediti per passata.
- Conseguenza: **il confronto SX-vs-Pinnacle in fase 2 deve leggere Pinnacle
dalla cache**, non chiamare l'API a ogni giro.

**4) GATE EV: UNA SOLA DEFINIZIONE.**
    EV = p_true x (quota - 1) - (1 - p_true)
Le due letture della direttiva **coincidono esattamente**:
    EV >= ev_min   <=>   quota >= true_odd x (1 + ev_min)
(`true_odd` = 1/p_true). `required_price` espone la seconda forma e un test
verifica l'equivalenza su ogni riga: e' l'invariante che impedisce due
standard diversi nella stessa pipeline. `ev_min` di default e' **lo stesso
`value_filter.EV_MIN` di produzione** (importato, mai copiato; tripwire).

**5) FAIL-CLOSED SCELTI (non default).**
- Per de-vigare un 1X2 servono **tutti e tre** gli esiti: con due su tre il
margine dell'esito mancante verrebbe attribuito agli altri in silenzio ->
  `None` (nessun oracolo invece di un oracolo distorto). Misurato: 62/65.
- Le righe con quota <= 1.0 sono scartate; `fair_odds` accetta solo
  probabilita' in (0, 1] (un valore > 1 invertirebbe il segno dell'EV).
- Un `price_lookup` che esplode **non** viene inghiottito: si CONTA
  (`totals["price_errors"]`) e si dichiara, perche' "lettura rotta" non deve
  leggersi come "zero value" (lezione del probe BTTS di oggi, dove
  `_discover_type` rendeva indistinguibili i due casi).

**6) TRIPWIRE (il pivot e' esplicito, e i test lo difendono).**
`test_pinnacle_api.py` (**47 verdi offline** + 1 live opt-in con
`PINNACLE_PROBE=1`) verifica che il modulo NON contenga: riferimenti al motore
statistico (`poisson_engine`, `expected_goals`, `prob_1x2`, `prob_btts`,
`ah_outcome_probs`, `ou_outcome_probs`), istruzioni di scrittura
(`save_prediction`/`save_bet`/`save_market_quotes`/`sqlite3`/`INSERT`/`UPDATE`),
ne' ordini (`_live_fill`/`place_order`/`resolve_market_for`/
`execution_engine`/`auto_bet`); che nessun import di rete stia a livello
modulo; e che **importare `pinnacle_oracle` non carichi** poisson/tracker/bot/
auto_bet/decision (verifica in sottoprocesso).

**7) CLI.**
  `venv/bin/python pinnacle_oracle.py --from-cache [--json]`  (0 crediti)
  `venv/bin/python pinnacle_oracle.py --live SPORT_KEY`          (1 credito)

**⚠️ STATO: NON collegato alla produzione.** `auto_bet` e `multi_market`
continuano col percorso attuale; il **bypass di Poisson non e' attivo** (e'
una decisione di pipeline, fase 2, non un effetto collaterale di una funzione
di lettura). Il branch `feature/top-down-pinnacle` **non e' pushato**: la
sessione si chiude in attesa dei dati di fine mese.
⚠️ Promemoria di lavorazione: il filtro **era/fascia quota** su
`shadow_report`/`market_diagnose` (sviluppato e verificato il 25/09: comando
`--since 2026-09-19 --odds-min 1.30 --odds-max 1.80` su entrambi, 164+171 test
verdi, 7 file +650/-18) e' **ancora nello `stash@{0}`** in attesa del push su
`main`: va ripreso da li', non riscritto.

**PROSSIMO PASSO (fase 2, da decidere):** collegare SX Bet come prezzo di
confronto (`price_lookup` iniettabile, gia' previsto dall'interfaccia) e
girare il confronto **sulle cache** per misurare quanti candidati value genera
il ritardo SX-vs-Pinnacle. Solo dopo ha senso parlare di bypass del modello e
di ordini.

### Espansione orizzontale (OU/BTTS): verdetto d'era + sentinella BTTS (25/09/2026)

Direttiva del proprietario: estrarre piu' valore dalle partite gia' analizzate
con il motore Poisson (O/U e BTTS), senza toccare i filtri di capitale (EV e
Edge restano congelati). Prima di scrivere codice e' stato valutato il backlog
sull'infrastruttura REALE; due delle premesse sono risultate **false** e i dati
hanno cambiato la decisione.

**1) O/U 2.5 era GIA' pronto end-to-end** (dal 19/09): `multi_market` copre
discovery SX type 2 -> contratto 2.0 -> `market_quotes` -> modello push-aware
(`ou_outcome_probs`) -> devig/blend -> `predictions` (esito `Over 2.5`) ->
`live_picks` -> `resolve_market_for`, con settlement line-aware. Il 2.5 e' una
semplice linea fra quelle gia' gestite. L'unico blocco era l'interruttore
(`ENABLE_LIVE_OU=0`) piu' la misura mancante.

**2) LA MISURA HA SMENTITO IL NUMERO CHE GIUSTIFICAVA L'ACCENSIONE.** Shadow
report in produzione (sola lettura) e isolamento della popolazione giocabile:

```
OU: 2050 quote | 158 chiuse + 98 aperte
  giocabili : 30 chiuse (15V/11P/4push), +6.36/unita' | ROI +21.21%
    · value        : 10 chiuse (7V/3P), +5.57/u | +55.70%
    · strong_value : 20 chiuse (8V/8P/4push), +0.79/u | +3.97%
  scartati  : 128 chiuse (21V/88P/19push), -7.16/u | -5.59%
AH (controllo): giocabili 4 chiuse +37.97% | scartati 118 chiuse -11.30%
```

**TRAPPOLA D'ERA (la stessa lezione del 22/09 sul 1X2, qui su un mercato
diverso)**: isolando le 30 giocabili per data di nascita:

| era | righe | quota media | risultato | ROI |
|---|---|---|---|---|
| **< 19/09** (`fixture_engine`, pipeline RITIRATA) | 22 | ~2.25 (oggi `rejected`: fuori fascia) | +7.24/u | **+32.9%** |
| **>= 19/09** (`multi_market`, la corsia di oggi) | **8** | 1.30-1.48 | 3V/2L/**3push**, -0.876/u | **-10.9%** |

Verifica: `7.24 + (-0.876) = 6.364 = 6.36` -> il +21.21% e' **interamente**
portato da una strategia ritirata. Le 8 righe dell'era nuova stanno tutte su
**linee intere alte (4 / 4.5 / 5)** con quota 1.30-1.48 e **3 push su 8**
(payout minimo, P/L quasi tutto push: ROI fragile per costruzione). Le 98
righe aperte sono TUTTE `rejected`: la popolazione giocabile non crescera' dal
backlog, solo dalla nuova copertura Tier-1.

**3) DECISIONE DEL PROPRIETARIO (registrata)**: `ENABLE_LIVE_OU` resta **0**
(mercato in sola simulazione) finche' non si contano **almeno 30 chiusure
refertate esclusivamente nell'era nuova (post 19/09) e con ROI positivo**. Non
si "compra campione" con denaro reale su una strategia in perdita con un
bankroll di 33.55 USDC. Corollario operativo: il collo di bottiglia per
decidere e' che **lo split per stato non isola l'era** — la rifinitura annotata
il 22/09 (filtro `--since`/fascia quota su `shadow_report`/`market_diagnose`)
resta da implementare, ed e' il prerequisito di qualunque lettura del report OU.

**4) BTTS — CONGELATO, con sentinella gratuita.** Scartata l'ipotesi del feed a
pagamento (the-odds-api `btts` costerebbe 2 crediti/chiamata: `markets x
regions`): non ha senso logico ne' economico erodere i crediti per rincorrere
un mercato che l'exchange non quota. Gli **10 punti di refactoring** del
backlog BTTS restano congelati. La quota BTTS NON viene da the-odds-api (dal
09/09 la fetcher e' `markets="h2h"` only): viene da SX Bet, che pero' **non
pubblica il type 17** — verificato con probe reali il 18/09 e di nuovo il
25/09/2026: **0 mercati** (mentre il type 2 Over/Under ne pubblica 100+).
Senza prezzo non esiste value bet: il modello la probabilita' la sa calcolare
(`prob_btts`), ma non c'e' nulla con cui confrontarla.

Implementato SOLO il campanello (nessun altro intervento):
- `multi_market.WATCHED_TYPES = {"BTTS": "17"}` +
  `probe_market_type()` / `probe_watched_markets()` / `format_probe()`;
- CLI `venv/bin/python multi_market.py btts` (verificato contro l'API reale:
  `BTTS: non disponibile (0 mercati)`);
- job `bot.btts_watch_job` giornaliero (24h, `first=1800`): logga SEMPRE lo
  stato, notifica admin+iscritti SOLO se il type 17 si popola, con anti-spam
  1 alert/giorno (chiave `BTTS_FEED`);
- **gratuito per costruzione**: solo `/markets/active` pubblico, nessuna chiave,
  nessun credito, nessun ordine (tripwire dedicato); BTTS resta **fuori** da
  `MARKETS`/`SX_TYPE_IDS`/`live_markets()` (test che lo blinda).
- **BUG trovato dai test**: `_discover_type` inghiottiva l'eccezione, quindi
  "probe rotto" e "0 mercati" erano **indistinguibili** (il campanello avrebbe
  taciuto proprio quando serve). Aggiunto il canale `errors` esplicito:
  `available=False` se la lettura fallisce. Nessun nuovo env, quindi nessuna
  modifica a `.railway/railway.ts`.

**5) Test**: `test_multi_market.py` **62 verdi** (10 nuovi su probe/fail-safe/
indipendenza), regressioni verdi su `test_bot`, `test_sx_signals`,
`test_auto_bet`, `test_secret_hygiene`, `test_liquidity_monitor`;
`compileall` OK, 0 marker di conflitto.

**6) INFRA**: la chiave host SSH di `ssh.railway.com` e' CAMBIATA (fingerprint
ED25519 nuovo) e bloccava le interrogazioni del container: rimossa la voce
stantia da `~/.ssh/known_hosts` (autorizzato dal proprietario;
`ssh-keygen -f ~/.ssh/known_hosts -R ssh.railway.com`). Nota permanente: dopo
la rimozione, la prima connessione non-interattiva puo' richiedere di
riaccettare l'host key.

#### Filtro d'ERA e di FASCIA QUOTA: il prerequisito (25/09/2026)

Direttiva del proprietario dopo il verdetto d'era sull'O/U: rendere la misura
ripetibile con un comando nativo, invece di doverla rifare a mano ogni volta.
**Implementato e verificato; NON ancora pushato** (in attesa del via libera).

**1) UNA SOLA DEFINIZIONE, NEL LEDGER** (`tracker.filter_predictions`):
- `tracker.filter_predictions(rows, created_since=, odds_min=, odds_max=)`;
- `tracker.get_predictions(..., created_since=, odds_min=, odds_max=)` e
  `tracker.predictions_summary(..., created_since=, odds_min=, odds_max=)`;
- **era = `created_at`** (quando il segnale e' NATO), con ripiego su
  `settled_at` solo se manca: separa due STRATEGIE, non due date di saldo.
  `created_since` resta distinto da `settled_since` (che esisteva gia'):
  sono due domande diverse e un test lo blinda.
- **fail-closed sul dato mancante**: con un filtro attivo una riga senza data
  (o con quota non leggibile) viene ESCLUSA — non si puo' dimostrare che
  appartenga alla popolazione richiesta. I due filtri sono INDIPENDENTI:
  manca la data -> fuori dal filtro d'era; manca la quota -> fuori dal filtro
  di fascia (una riga con data valida non sparisce perche' le manca la quota).
- **date normalizzate in PYTHON** (mai confronti SQL fra date): lezione del
  17/09 — il ledger usa ISO con la 'T', SQLite produce lo SPAZIO, e a parita'
  di giorno la riga risulterebbe piu' nuova del cutoff. Verificati 'T', 'Z',
  offset, microsecondi e spazio.

**2) I DUE CONSUMATORI** (nessuna copia della logica):
- `multi_market.shadow_report(since=, odds_min=, odds_max=)` usa
  `filter_predictions`; il report lo **DICHIARA SEMPRE** (riga `Filtro:` + blocco
  `filter` nel JSON con `rows_total`/`rows_kept`/`rows_excluded`), e senza
  filtro scrive esplicitamente `Filtro: NESSUNO — mescola ere e strategie
  diverse (usare --since ...)`: un report filtrato e uno completo non devono
  essere indistinguibili.
- `market_diagnose.analyze_db(since=, odds_min=, odds_max=)` passa il filtro a
  **ENTRAMBE** le letture (giocabili e totale): se il blocco `excluded` fosse
  calcolato su un'altra popolazione, giocabili ed esclusi non sarebbero piu'
  complementari e un pezzo di ledger sparirebbe dai conti (test dedicato).

**3) CLI**: `multi_market.py report --since 2026-09-19 --odds-min 1.30
--odds-max 1.80` e (stessi flag) `market_diagnose.py`. Nessuna env nuova,
quindi nessuna modifica a `.railway/railway.ts`; i default restano INVARIATI
(senza flag il comportamento e' quello storico, e i test preesistenti passano).

**4) VERIFICA SUI DATI REALI (senza deploy):** moduli e uno SNAPSHOT del DB
copiati in `/tmp/era` del container (`QUOTAVERACE_DATA_DIR=/tmp/era
PYTHONPATH=/app`), produzione non toccata, `/tmp` rimosso a fine misura.

```
SENZA FILTRO   OU giocabili: 30 chiuse (15V/11P/4push) | ROI +21.21%   <- inquinato
CON FILTRO     Filtro: era dal 2026-09-19 | quota 1.3-1.8 -> 31 righe tenute su 456 (425 escluse)
               OU giocabili:  8 chiuse (3V/2P/3push) | ROI -10.95%
               AH giocabili:  4 chiuse (3V/0P/1push) | ROI +37.97%
market_diagnose filtrato: 13 chiusi giocabili | ROI -2.75% | EV atteso +16.24 | gap -18.99
               OU  n=8  ROI -10.95%  probGap -15.1  <- overconfidence
               AH  n=4  ROI +37.97%  probGap +32.8
               1X2 n=1  ROI -100%    (riga singola dell'era nuova)
               fuori dai calcoli: 73 righe non giocabili, dichiarate
```
Il `-10.95%` riproduce esattamente la misura manuale del 25/09 (-10.9%): il
comando nativo e' ora la fonte di verita' per la soglia delle **30 chiusure
post-19/09 con ROI positivo** decisa dal proprietario per `ENABLE_LIVE_OU`.

**5) Test**: 128 verdi sui tre file (`test_predictions.py`,
`test_multi_market.py`, `test_market_diagnose.py`) + 171 di regressione
(`test_reports`, `test_performance_report`, `test_ml_*`, `test_dedup_ml`,
`test_auto_bet`, `test_decision_compare`, `test_league_gate_impact`,
`test_flow_measure`); `compileall` OK, 0 marker di conflitto.
⚠️ Un mio test aveva l'aspettativa sbagliata (pretendeva esclusa dal filtro
d'era una riga con data VALIDA ma senza quota): corretto separando i due
filtri — il codice era giusto, il test no.

### Fase 2 — Integrazione del comparatore + Dry-Run (25/09/2026, sera)

Direttiva del proprietario, quattro passi in sequenza: (1) pop dello stash,
(2) EV top-down da `pinnacle_oracle` bypassando il Poisson, (3) flag
dry-run che intercetta l'ordine prima del POST a SX Bet, (4) commit e push
del branch `feature/top-down-pinnacle`.

**1) STASH RECUPERATO (con un'intuizione di sequenza).** Il pop diretto era
bloccato da AGENTS.md modificato in working tree (la Fase 1 non era ancora
committata): committata prima la Fase 1 (`dcc9699`), poi il pop e' filato
liscio (base dello stash = `5fb4471` = HEAD del branch, AGENTS.md si e'
fuso da solo: 0 marker di conflitto). Nel branch ora c'e' ANCHE il filtro
era/fascia quota (`tracker.filter_predictions` + CLI `--since/--odds-min/
--odds-max`), il prerequisito per leggere il campione OU dell'era nuova.
Test del codice recuperato: 128 verdi (test_predictions, test_multi_market,
test_market_diagnose).

**2) ORACOLO PER PARTITA DALLE CACHE — `pinnacle_oracle.load_oracle(home,
away, sport_key=None)` (0 crediti, nuovo blocco 3 del modulo).**
- Legge le STESSE cache della rotazione quote (`toa_<sport>.json`), ordinate
  per freschezza; match per SOTTOSTRINGA case-insensitive di ENTRAMBE le
  squadre sulla STESSA riga del payload (i nomi the-odds-api del segnale
  coincidono con quelli del payload: stessa fonte; la sottostringa copre
  "Tottenham" vs "Tottenham Hotspur").
- Fail-closed: None se Pinnacle non ha i TRE esiti 1X2 (de-vig su 2 su 3
  distorcerrebbe), o se la cache e' piu' vecchia di `CACHE_MAX_AGE_H`
  (default 24, env `PINNACLE_CACHE_MAX_AGE_H`) — un oracolo stantio non e'
  il mercato.
- Memo di lettura su (mtime, size): il giro gira ogni 60s e piu' pick
  condividono la stessa lega; una cache riscritta si auto-invalida e una
  cartella diversa (test) non condivide nulla.
- `sport_key` opzionale restringe la lettura a una cache (nessun uso oggi).
- Tripwire Fase 1 intatti: la direzione e' auto_bet -> oracolo (il modulo
  NON menziona il percorso ordini: un primo tentativo di docstring lo
  faceva ed e' stato corretto PRIMA del push).

**3) EV TOP-DOWN NEL GIRO ORDINI (`auto_bet._top_down_eval` + wiring FASE 1
di `run_today_bets`, env `TOP_DOWN_EV` default ON, `TOP_DOWN_MARGIN` 0.02).**
- La p_true arriva da `load_oracle` (loader iniettabile `auto_bet.
  _top_down_load`); la quota del segnale resta il prezzo; l'EV e' quello
  dell'oracolo: `EV = p_true x (quota-1) - (1-p_true)`. Le probabilita' del
  modello nel pick (`market_prob`, `best_ev`) NON entrano nella decisione
  (test dedicato: EV invariato anche con prob. modello assurde).
- Soglia EV UNA: `value_filter.EV_MIN` (l'oracolo importa la STESSA
  costante). `required_price = true_odd x (1+TOP_DOWN_MARGIN)` e' la
  seconda forma della STESSA condizione; se le due letture divergono il
  codice LOGGA (non "sistema"): una divergenza e' un bug del gate.
- Gate applicato SOLO sulla corsia LIVE (`mode == "live"`): la corsia paper
  SIM mantiene la base storica del segnale per non cambiare era al ledger
  che alimenta ML/CLV (lezione 22/09). Fail-closed: segnale senza oracolo
  (`no_oracle`) NON si ordina — senza verita' non c'e' ritardo da comprare.
- ⚠️ In produzione il gate diventa attivo SOLO quando esiste
  `AUTO_BET_MODE=live` + provider pronto: in SIM il percorso resta quello
  storico (il log diagnostico del candidato top-down esce comunque in
  dry-run, vedi sotto).

**4) DRY-RUN (`auto_bet.DRY_RUN`, env `AUTO_BET_DRY_RUN`, default OFF).**
- In `run_today_bets` (FASE 3): il candidato che ha superato TUTTI i gate
  (top-down EV, timing, cap, feed, liquidita') viene LOGGATO a WARNING con
  match, esito, quota, stake, EV top-down e mode, e SALTATO: NESSUN POST
  all'exchange, NESSUNA riga sul ledger `bets` (un ordine non piazzato non
  deve sembrare piazzato). Riepilogo finale dedicato ("N candidati ...
  INTERCETTATI").
- Seconda barriera in `_live_fill` (difesa in profondita'): col flag attivo
  ritorna None PRIMA di costruire l'engine — nessun POST a SX possibile da
  qualunque chiamante (test diretto).
- Default OFF: il comportamento di produzione e' invariato finche' il
  proprietario non accende `AUTO_BET_DRY_RUN=1` (in IaC gia' dichiarata).

**5) TEST.** `test_top_down.py` (nuovo, 32 verdi offline: load_oracle con
cache finte — p_true/fair/longshot/varianti nome/fail-closed 3 su 3/stantia/
sottostringa senza fusione/zero HTTP; valutatore — EV sull'oracolo, trigger,
bypass modello, no_oracle, soglia unica, equivalenza delle due letture su
griglia; wiring — dry-run intercetta, EV basso non arriva all'esecuzione,
top-down spento riprende il percorso storico, log dry-run con i dettagli,
_live_fill bloccato; tripwire — IaC documentata, oracolo senza riferimenti
al percorso ordini). Aggiornati in modo dichiarato: `test_auto_bet_live.py`
(stub oracolo nel fixture autouse: quei test misurano staking/cap, non l'EV;
la semantica del gate e' in test_top_down) e `verify_guardrails.py` (stub
dichiarato: lo scenario C passerebbe per `no_oracle` invece che per il cap
severo — ora la controprova col floor funziona davvero).
Regressioni verdi: test_auto_bet x3 + favourites_only + t60_breakers (122),
test_value_filter + risk_guards + league_gate + multi_market + market_calib
+ ou_exclusion + secret_hygiene + settlement_pause + predictions +
market_diagnose (198), test_bot + web_api + liquidity_monitor +
flow_measure + reports + tier (105), test_decision_limits + compare +
shadow + execution_engine (110). `verify_guardrails.py`: A-G bloccavano
(dal 26/09 lo scenario D e' ROSSO — aspettativa del bypass, NON una
regressione: vedi la sezione di chiusura del 26/09). `compileall` OK.
`railway config plan`: 0 to add, 1 to change, 0 to destroy.

**6) IaC.** `TOP_DOWN_EV`, `TOP_DOWN_MARGIN`, `AUTO_BET_DRY_RUN` dichiarate
`preserve()` nel blocco api di `.railway/railway.ts` (accanto ai T60_*).
⚠️ NON impostate su Railway: valgono i default di codice (gate ON, dry-run
OFF). Nota operativa: `PINNACLE_CACHE_MAX_AGE_H` resta di codice (non e'
necessaria in preserve finche' non si tara).

**7) STATO.** Branch `feature/top-down-pinnacle` **FUSO su `main`**
(`origin/main` = `49b7f9e`): il gate top-down governa la corsia live come
progettato. Lettura rapida del flusso: `venv/bin/python
pinnacle_oracle.py --from-cache` (0 crediti) e `AUTO_BET_DRY_RUN=1` per
vedere i candidati intercettati nei log. Il branch e' stato **eliminato da
locale e da origin** il 26/09 (su origin resta SOLO `main`): gli interruttori
e i dettagli operativi sono nella sezione di chiusura del 26/09.

### Chiusura del giro top-down + pulizia repo (26/09/2026)

Direttive del proprietario a chiusura del giro, con lo stato dichiarato.

**1) `TOP_DOWN_EV` RESTA ACCESO (nessuna modifica di codice).** Il gate EV
sull'oracolo Pinnacle (fail-closed senza oracolo) governa il prezzo sulla
corsia live: **default di codice ON** (`os.getenv("TOP_DOWN_EV", "1")`),
non impostato su Railway -> vale il default. Scelta dichiarata: "meno
scommesse ma con un margine confermato"; se il volume va a zero si fa tuning,
il filtro non si tocca. Le altre leve top-down restano come dal 26/09:
`TOP_DOWN_MARGIN` 0.02, **`TOP_DOWN_BYPASS` default SPENTO** (il bypass della
fascia 1.30-1.80 NON governa ordini reali senza una scelta esplicita),
`AUTO_BET_DRY_RUN` default OFF. Tutte `preserve()` in `.railway/railway.ts`.

**2) MODALITA' SIM INVARIATA (nessuna modifica).** Le righe paper restano
etichettate `mode='sim'`, non toccano il bilancio reale e continuano ad
alimentare storico/ML/CLV. E' gia' il comportamento attuale.

**3) DOCUMENTAZIONE.** Questa sezione + rimozione delle note obsolete sul
blocco del deploy (Fase 2 §7 non dice piu' "non merged") e aggiornamento del
claim `verify_guardrails` (Fase 2 §5).

**4) PULIZIA REPO (fatta).** Il lavoro top-down era **gia' su `main`**:
`origin/main` = `49b7f9e` (Fase 1 `dcc9699` + Fase 2 `696f899` + i tre commit
del giro successivo). Il `main` **locale** era stale (`5fb4471`): allineato con
fast-forward. `origin/feature/top-down-pinnacle` era fermo a `696f899` e
**interamente contenuto in main** (0 commit avanti) -> **eliminato da origin**
(`git push origin --delete`, via askpass del vault) e localmente. Su origin
resta SOLO `main`: nessun ramo morto. Il push su `main` e' comunque il deploy
(regola di progetto).

**5) TRE COMMIT NON ANCORA DOCUMENTATI IN MEMORIA (ora lo sono).**
- `a18806d` — **risposta HTTP cruda nei log + `orderId` obbligatorio**: il
  ledger poteva registrare un "successo" senza che sull'exchange esistesse un
  ordine. `SxBetProvider._post` ora logga la risposta grezza (troncata, con la
  firma EIP-712 mascherata per nome di campo) e `place_limit_order` tratta un
  `FULLY_FILLED` **senza `orderId`** come NON piazzato (fail-closed).
- `1de63db` — **corsia top-down** (`auto_bet._top_down_picks`): la corsia LIVE
  pesca da ogni riga 1X2 aperta e usa l'oracolo come unico giudice del prezzo;
  `TOP_DOWN_BYPASS` **default SPENTO** per non far girare ordini reali fuori
  fascia senza decisione esplicita (contro il congelamento del 22/09);
  prudenza extra in probation (`TOP_DOWN_PROBATION_EXTRA` 0.02); **dedup
  cross-corsia** per `(match_id, esito_key)` (senza, lo stesso evento veniva
  ordinato DUE volte prima che esistesse la riga `bets`); guardia sul `bet_id`
  (niente status "SUCCESS" inventato).
- `49b7f9e` — **stessa guardia `bet_id` sul dispatch T-60**, il SECONDO punto
  di scrittura di una riga `mode='live'`: senza un id emesso dall'exchange la
  riga non nasce (difesa in profondita', fail-closed).
Test aggiornati nello stesso giro: `test_execution_engine.py`,
`test_top_down.py` (+225 righe), `test_auto_bet_live.py`,
`test_t60_breakers.py`.

**6) `verify_guardrails.py` SCENARIO D — ROSSO, IN BACKLOG (confermato).** Lo
scenario D ("filtro prezzo: fascia bottom-up in SIM vs gate oracolo in LIVE")
**non accende `TOP_DOWN_BYPASS`** e assume il default del 25/09 (bypass ON):
col default SPENTO del 26/09 la corsia top-down non aggiunge quote fuori
fascia e l'asserzione `ok_bypass` non passa. E' **una aspettativa dello
scenario, non una regressione dei guardrail** (A/B/C/E/F/G bloccano
regolarmente, misurato il 26/09). Fix naturale quando si vorra': che lo
scenario accenda il bypass **esplicitamente** per la sua parte LIVE, con
un'asserzione separata sul default spento. **Non eseguito** (backlog).

**7) Stato git**: `main` = `49b7f9e`, unico ramo su origin, nessuna env nuova
(nessuna modifica a `.railway/railway.ts` in questo giro).

### Live Mode: CB2 allargato a 25 USDC + gate di PRONTEZZA dell'OU (26/09/2026)

Direttiva del proprietario: "esecuzione reale immediata" — dry-run spento,
ordini effettivi sbloccati sia per le linee standard sia per la copertura
ampliata AH/OU, deploy e riscontro coi log. Prima di applicare sono state poste
due domande (denaro reale + conflitto fra due direttive); risposte registrate:
**CB2 a 25 USDC** e **OU live solo dopo N chiusure**.

**1) DRY-RUN: GIA' SPENTO, NESSUNA AZIONE.** Verificato sul container:
`auto_bet.DRY_RUN` = **False**, `kill_switch_status()` =
`{effective: 'live', provider_ready: True}`, provider = `SxBetProvider` (mai
`DryRun`). `AUTO_BET_DRY_RUN` non e' impostata su Railway -> vale il default di
codice (OFF). La direttiva "disattiva il dry-run" era quindi **gia' soddisfatta**;
`top_down_ev` attivo di default, `TOP_DOWN_BYPASS` spento (default del 26/09).

**2) CB2 ALLARGATO A 25 USDC** (`T60_KILL_WALLET_USDC=25`, env Railway +
`preserve()`). Equity reale misurata: `33.5535` (liberi 33.5535 + in gioco
0.00), CB2 non armato, `daily_stop` solo riferimento (`basis_key live_equity`).
Con la soglia a 30 il margine era **3.5 USDC = ~3 bet perse da 1 USDC** prima
dell'arresto; a **25** il margine diventa **~8.5 USDC (~8 bet)** e resta una rete
di sicurezza reale (~75% del capitale protetto dal floor). Scelta del
proprietario fra 15/20/25/30.

**3) AUTORIZZAZIONE ≠ ORDINI — gate di PRONTEZZA dell'OU (`multi_market.py`).**
Conflitto esplicito: la direttiva "abilita AH/OU subito" contraddiceva il veto
del 25/09 ("OU live solo con >= 30 chiusure post-19/09 e ROI positivo"); la
misura reale e' **8 chiusure, ROI -10.95%** (linee intere alte, 3 push su 8).
Il proprietario ha scelto la **soglia intermedia: 20 chiusure**. Implementato:
- `authorized_markets()` = cio' che gli interruttori ACCENDONO (intenzione);
- `ou_readiness()` = misura del campione dell'ERA (`OU_LIVE_SINCE` 19/09/2026) e
  della FASCIA QUOTA (`ODDS_MIN`-`ODDS_MAX`), sulle sole righe GIOCABILI e
  CHIUSE (`tracker.filter_predictions` + `PLAYABLE_TIERS`: definizioni uniche
  riusate, mai copiate). `ready` solo con >= `OU_LIVE_MIN_CLOSURES` (20)
  chiusure **E** ROI positivo; `reason` sempre dichiarato;
- `ou_live_ready()` con memoria TTL (`OU_READY_TTL` 60s: il giro gira ogni 60s e
  `live_markets()` e' chiamata anche per candidato), `reset_ou_ready_cache()`;
- `live_markets()` = autorizzati ∩ pronti. **Definizione unica** letta da
  `live_picks`, `shadow_report` e da `auto_bet` per i log: una corsia non puo'
  essere accesa in un percorso e spenta in un altro. L'OU **parte da solo**
  quando la soglia e' raggiunta — nessun intervento manuale.
- FAIL-CLOSED: se la misura non e' disponibile (ledger assente/corrotto) l'OU
  **non** e' pronto; un'incertezza non apre ordini reali.
- Il report dichiara sempre il PERCHE': `authorized_markets` + `ou_readiness` in
  `shadow_report`, riga `⏳ OU autorizzato ma NON pronto agli ordini: <motivo>` in
  `format_report` (senza, "OU in shadow" e "OU non pronto" sono indistinguibili)
  e nuovo comando CLI **`venv/bin/python multi_market.py ou [--json]`**.

**4) COPERTURA AH/OU (gia' deployata il 26/09, commit `01cd125`).** Discovery
600 mercati grezzi (era 400) e 20 linee per mercato (era 12): si allarga SOLO la
copertura, fascia quota/edge/EV/gate di lega restano quelli congelati del 22/09.

**5) ENV.** Su Railway: `T60_KILL_WALLET_USDC=25`, `ENABLE_LIVE_OU=1` (autorizza;
il gate decide), `OU_LIVE_MIN_CLOSURES=20`. Dichiarate in `preserve()` di
`.railway/railway.ts` (`OU_LIVE_MIN_CLOSURES`, `OU_LIVE_SINCE`, `OU_READY_TTL`
accanto a `ENABLE_LIVE_OU`). ⚠️ `ENABLE_LIVE_OU=1` **non** significa ordini OU:
con 8 chiusure su 20 richieste l'OU resta shadow e lo dichiara nei log.

**6) ISOLAMENTO NEI TEST.** `conftest.py` azzera la memoria di prontezza
(prima e dopo ogni test): e' stato a livello di MODULO e sopravvive fra i test
dello stesso processo, mentre il ledger no (ogni test ha il suo DB temporaneo) —
senza reset un caso "pronto" avrebbe abilitato l'OU per tutti i successivi.

**7) TEST.** `test_multi_market.py` **92 verdi** (11 nuovi in
`TestProntezzaOU`: autorizzazione/prontezza distinte, OU autorizzato ma non
pronto -> `live_picks() == []`, abilitazione automatica al raggiungimento della
soglia, campione insufficiente, ROI non positivo, soglia da env, solo i
giocabili dell'era contano, fail-closed su DB non leggibile, memoria+reset,
report che dichiara il motivo, comando CLI esposto e modulo senza `_live_fill(`/
`place_order(`). `test_ou_live_solo_col_suo_interruttore` isola la prontezza
(`ou_live_ready`) per non mescolare due responsabilita'. Regressioni verdi: 391
(multi_market, sx_signals, book_flow, auto_bet x2, t60_breakers, risk_guards,
favourites_only, league_gate, liquidity_monitor, settlement_pause,
secret_hygiene, decision_feed) + 356 (bot, market_diagnose, predictions,
flow_measure, value_filter, market_calib, tier, decision_shadow/compare/
limits/pipeline, web_api, reports, poisson_engine).

**8) ESITO DEL DEPLOY E RISCONTRO DAI LOG (26/09/2026, 01:5x UTC).**
Commit `ffd45be` -> deploy **`90e4b98f` SUCCESS**, poi env applicate -> redeploy
**`15a06f17` SUCCESS**, health 200. Verificato SUL CONTAINER:
`authorized = ('AH','OU')`, `live = ('AH',)` (l'OU non e' pronto),
`CB2 soglia 25.0` non armato, `DRY_RUN False`, wallet `33.5535` (equity).
Il gate ha misurato sui dati REALI esattamente il numero documentato:
**8 chiusure su 20, ROI -10.95%** -> `ready=False`.

**⚠️ 0 ORDINI, e nessuno e' un blocco tecnico: sono DUE gate fail-closed.**
Il ciclo `auto_bet` (ogni 60s) logga, in UTC:
`bankroll LIVE = equity 33.55 USDC` -> `1 pick multi-mercato dalle corsie live
(AH)` -> `sx-L20214934 (Home +0.5) top-down SKIP [no_oracle]: Pinnacle
assente/incompleto/stantio (fail-closed...)` -> `nessuna puntata (live) — 0
candidati giocabili`.
1. **`no_oracle`**: il pick e' Real Salt Lake-New England (MLS), ma la cache
   Pinnacle MLS (`toa_soccer_usa_mls.json`) ha **24.6h** contro il limite
   `PINNACLE_CACHE_MAX_AGE_H=24.0` -> oracolo stantio -> skip. **Tensione
   STRUTTURALE da decidere**: la rotazione delle leghe ammesse e' a **2 giorni**
   (25/09) mentre la freschezza dell'oracolo e' **24h**, quindi l'oracolo manca
   per circa meta' del tempo. MLS e' `is_sport_due = True` -> il job delle 04:00
   UTC lo rinfresca.
2. **Fuori finestra T-60**: `t60_window` = `before`; la finestra esecutiva
   (T-120..T-50) si apre alle **23:30 UTC** e chiude alle **00:40 UTC** del
   27/09. Nessun ordine e' possibile prima, per costruzione.
→ Il **primo tentativo reale** avverra' nella finestra di stanotte: a
quell'ora la cache MLS rinfrescata dalle 04:00 avra' ~19.5h (< 24h) e l'oracolo
sara' disponibile. Copertura ampliata confermata viva: `ingest 424 mercati ->
664 quote salvate (30 fixture)`, `28 fixture con quote, 8 segnali giocabili
(1 nelle corsie live)`. Crediti 392 (reset 01/10, consumo misurato 15/giorno su
98/giorno sostenibili).

### Consenso multi-oracolo per l'EV: Pinnacle + Betfair, Matchbook validatore (26/09/2026)

Direttiva del proprietario: l'EV non si calcola piu' sul prezzo secco di UNA
fonte. `pinnacle_oracle.py` estrae TUTTE le fonti sharp disponibili dalla
stessa cache che la rotazione quote scarica gia' (**zero crediti, zero rete,
zero ordini**) e le aggrega in un **consenso de-vigato**.

**Fonti e ruoli** (tabelle esplicite, mai fuzzy):
- **PRIMARIA** `pinnacle`; **BENCHMARK** `betfair_ex_eu` (il pattern
  `betfair` copre anche `_uk`); **VALIDATORE** `matchbook`.
- `MULTI_BOOKMAKERS = "pinnacle,betfair_ex_eu,matchbook"`: e' l'array usato
  dalla chiamata `/odds` del percorso `--live` (`fetch_pinnacle_payload`).
  ⚠️ the-odds-api addebita `markets x regions`: filtrare i bookmaker **non**
  costa di piu' e riduce il payload.
- **SCELTA ESPLICITA**: il fetch PRINCIPALE (`odds_api._get_odds`, quello
  della rotazione che alimenta `fixture_engine`/line shopping) **NON e'
  toccato** — resta `regions=eu` senza filtro, per non cambiare quota/edge dei
  segnali gia' in produzione. Il consenso si calcola solo dentro l'oracolo.

**Algoritmo** (`consensus_probabilities(quotes_by_book)`):
1. per ogni fonte disponibile con 1X2 COMPLETO, de-vig con la stessa
   `true_probabilities` del progetto (default `power`, favourite-longshot);
2. **benchmark** = aggregato di Pinnacle + Betfair (media `mean` di default,
   o `median`), rinormalizzato a somma 1;
3. **validatore** Matchbook: entra nell'aggregato **solo se** la sua fair resta
   entro `PINNACLE_VALIDATOR_TOLERANCE` (default 5pp) dal benchmark; se
   diverge troppo viene ESCLUSO e il disallineamento e' registrato
   (`validated=False`, `agreement_pp`);
4. **fallback robusto** (richiesto dalla direttiva): senza Betfair/Matchbook
   il consenso ripiega su cio' che c'e'. Con la sola Pinnacle il risultato
   **coincide bit per bit col comportamento storico** (`fallback:
   "pinnacle_only"`), quindi la pipeline non si blocca mai. `None` solo se
   NESSUNA fonte ha i 3 esiti.

**Integrazione EV** (`auto_bet._top_down_eval` -> `_top_down_load` ->
`load_oracle`): la p_true e' il consenso; il resto e' INVARIATO (stessa soglia
`value_filter.EV_MIN`, stesso floor EV, stesso fail-closed `no_oracle`). Il
verdetto espone `oracle_sources`, `oracle_validated`, `oracle_fallback`.

**Env** (`preserve()` in `.railway/railway.ts`, default di codice attivi):
`PINNACLE_CONSENSUS` (1=ON; `0` ripristina la Pinnacle-secca), 
`PINNACLE_CONSENSUS_METHOD` (mean|median), `PINNACLE_VALIDATOR_TOLERANCE`
(0.05), `PINNACLE_DEVIG_METHOD` (power).

**Diagnostica**: `venv/bin/python pinnacle_oracle.py --from-cache` ora misura
anche la copertura del consenso (`with_consensus`, `with_multi`) e dichiara
fonti/metodo/tolleranza. `scan_cache`/gate riportano `consensus_books`.

**Test**: `test_pinnacle_api.py` + classe `TestConsensoMultiOracolo` (media,
mediana, validatore che conferma/esclude, tolleranza configurabile, fallback
solo-Pinnacle / validator-only / benchmark-senza-Pinnacle, consenso disabilitato,
metadati esclusi da EV/true-odd, canonical_book, `load_oracle` dal consenso) +
`test_top_down.TestConsensoNelGateEv` (l'EV usa il consenso e differisce dalla
Pinnacle secca; fallback pinnacle_only). Tripwire del modulo intatti (nessun
Poisson, nessuna scrittura, nessun ordine, nessuna rete all'import).

### Quattro moduli avanzati: CB settimanale, adaptive weighting per lega, smart hedging (26/09/2026)

Direttiva del proprietario (4 moduli collegati a DB/Telegram). Il modulo 2
(riconciliazione risultati/P&L) esisteva gia' completo (`settle_bets`/
`settle_predictions`/`settle_cassa` + `bot._update_results` + watchdog 4h):
nessun codice richiesto. Gli altri tre, tutti con env in `preserve()` IaC:

**MODULO 1 — RISK MANAGEMENT: circuit breaker SETTIMANALE (`auto_bet.py`,
`decision/`).** Drawdown ROLLING 7gg (168h) su `BANKROLL_HISTORY_FILE`
(campioni max 1/h da `record_bankroll_sample`, chiamato nei giri dove il
bankroll e' determinato): oltre `WEEKLY_STOP_DRAWDOWN` (default 12%) le
puntate sono bloccate 24h (`WEEKLY_STOP_BLOCK_HOURS`, stato persistente
`WEEKLY_STOP_FILE`, re-arm AUTOMATICO a scadenza). `check_weekly_stop` in
`run_today_bets` SUBITO DOPO il daily stop; notifica Telegram anti-spam
1/giorno (chiave `WEEKLY_STOP`) + riga di stato in `/autobet`. Catena
`decision/`: nuovo `ReasonCode.WEEKLY_STOP_LOSS`, precedenza blocchi
manual > daily > weekly > settlement_pause (guard precedence 3, probe
kill_switch). Isolati in `conftest.py` (WEEKLY_STOP_FILE,
BANKROLL_HISTORY_FILE in tmp). Test: `test_weekly_stop.py` (~30 verdi).

**MODULO 3 — ADAPTIVE WEIGHTING per campionato (`adaptive_weighting.py`,
riscritto; default OFF).** Moltiplicatore di stake per lega dal CLV ROLLING
30gg (`clv_history` JOIN `predictions` JOIN `matches`; league =
COALESCE(NULLIF(TRIM(p.league),''), NULLIF(TRIM(m.league),'')); confronti
SQL su date AVVOLTI in `datetime(col)`; CLV grezzo sig/clos-1, quote <= 1
scartate; campione minimo 8). Env lette A RUNTIME (`adaptive_weighting.*`):
`ADAPTIVE_WEIGHTING_ENABLED` (default **0**), window 30, min_samples 8,
`ADAPTIVE_WEIGHTING_FLOOR` 0.5, restrict_threshold -0.04, ttl 300s
(+ `reset_cache`). SOLO riduzione (`_multiplier_from_clv` lineare fino al
FLOOR, mai > 1), `league_multiplier()` fail-open 1.0. Wiring:
`auto_bet._league_multiplier(league)` (import pigro, fail-open) nel ramo
adaptive staking DOPO il movement bonus e PRIMA del check stake <= 0 —
non applicato alla corsia flat. Test: `test_adaptive_weighting.py` (35 verdi,
tripwire IaC + sorgente inclusi).

**MODULO 4 — SMART HEDGING pre-match (`smart_hedging.py`, nuovo; default ON).**
Quando la quota di una posizione LIVE aperta si ACCORCIA di >=
`HEDGE_MIN_MOVE_PCT` (5%) entro la finestra `HEDGE_MIN_MINUTES` (10)..`HEDGE_HORIZON_H`
(24) dal kickoff, contropunta i DUE complementari: gambe
`H_i = f x S x Oe / O_i` (`HEDGE_FRACTION` 1.0), payout uguale su TUTTI gli
esiti, `locked_profit = min(payouts)` (peggiore onesto: con f<1 puo' essere
negativo -> `no_lock`), richiede `locked_roi >= HEDGE_MIN_LOCK_PCT` (1%).
- **Detection** (`find_opportunities`): solo 1X2 canonico (altrimenti
  `not_1x2`), solo bet `mode='live'` aperte, trigger = move_pct <= -soglia
  (mai contro di noi). **Guardia `already_open` ANCHE in detection** (non
  solo al fill): se sul ledger esiste una gamba aperta sul complementare la
  posizione NON viene riproposta — senza, il job ogni 15' ri-coprirebbe la
  stessa bet all'infinito e le gambe hedge stesse (bet live X/2) verrebbero
  valutate come posizioni (hedge dell'hedge). `complement_of()` e' la mappa
  1<->2 usata dalla guardia. Fail-closed su lettura fallita (il fill
  ri-controlla).
- **Esecuzione** (`place_hedge`): DELEGA a `auto_bet._live_fill` (tripwire:
  niente place_limit_order/OrderResult/EIP-712 nel sorgente), gambe NON
  atomiche (una puo' andare e l'altra no: JSONL lo dice), guardia
  `_bet_row_exists` fail-closed (riga aperta O chiusa blocca — `save_bet`
  e' UNIQUE(match_id, esito) con UPDATE solo su riga aperta), registrazione
  ledger DENTRO place_hedge (`save_bet` mercato='1X2', mode='live') e
  SOLO se >= 1 gamba e' ok. Le gambe si saldano col settlement esistente
  (`_prediction_outcome` gestisce esiti canonici). Blocchi via
  `_blocked_reason()`: kill_switch != live / daily / weekly stop /
  `stato_non_leggibile` (fail-closed). DRY-RUN rispettato (skip + log).
- **Telemetria**: JSONL `HEDGE_LOG` (eventi opportunity/placed/skip con
  reason machine-readable), `summary`, `format_alert`, `format_report`, CLI
  `--opportunities|--run|--report|--json`. Isolato in `conftest.py`.
- **Wiring bot.py** (26/09): job `hedge_job` ogni 15' (primo giro 210s,
  max_instances=1, `SMART_HEDGING=0` per spegnerlo) che chiama
  `run_hedge_cycle()` e invia `format_alert(entry)` a iscritti+admin PER OGNI
  hedge PIAZZATO (ordine reale = come le notifiche FULLY_FILLED, niente
  anti-spam sugli ordini; gli scarti restano su log/JSONL) + comando admin
  **`/hedge`** (stato via `format_report`).
- Env (tutte preserve()): SMART_HEDGING ("1"), HEDGE_MIN_MOVE_PCT (0.05),
  HEDGE_MIN_LOCK_PCT (0.01), HEDGE_FRACTION (1.0), HEDGE_MIN_STAKE_USDC (1.0),
  HEDGE_MAX_STAKE_USDC (5.0), HEDGE_MIN_MINUTES (10), HEDGE_HORIZON_H (24),
  HEDGE_LOG (default data/execution/hedge_events.jsonl).
- Test: `test_smart_hedging.py` (57 verdi, offline: fill/price_lookup
  iniettati, DB temporaneo).

**MIGRAZIONE REPO COMPLETATA (26/09/2026, direttiva del proprietario).** Remote origin
spostato su `https://github.com/siryo009/betting_bot` (nuovo account GitHub
`siryo009`; il vecchio era `Siryochy/quotaverace`) e push `git push -u origin
main` RIUSCITO: commit `efe5d79` ("Update: Full market coverage & Repo
migration", 15 file, i 4 moduli avanzati) e intera storia su origin/main. Percorso
dell'autenticazione: la repo `bot_bet` (URL iniziale indicata dal proprietario) non
esisteva; la repo reale e' `betting_bot` (PUBBLICA); il primo push e' fallito due
volte (404 con il token del vecchio account sulla repo inesistente; 403 "Permission
denied to Siryochy" sulla repo nuova) finche' il proprietario ha creato un
fine-grained PAT del nuovo account. Il token e' entrato nel vault con MERGE
esplicito (load_vault -> sostituzione GITHUB_TOKEN -> riscrittura atomica chmod 600,
5/5 segreti preservati, MAI `vault --commit`) e il plaintext e' stato distrutto con
`shred -u`. L'askpass passa il token nuovo: nessun credential helper di sistema
(check pulito), `gh` NON installata.

### RIPUNTAMENTO COMPLETATO + CRASH LOOP RISOLTO (27/09/2026) — NUOVA INFRASTRUTTURA

**Cronologia del guasto (27/09, diagnosi in sola lettura).** Dalle 15:16 UTC il
container crash-loopava con `ValueError: Token non configurato.` (`bot.py:2650`,
riavvii ogni ~1,3s). CAUSA RADICE DOPPIA, emersa solo con verifiche mirate:
1) **Il vecchio account Railway (`peppe85palermo`, progetto `quotaverace`) era
   andato in TRIAL EXPIRED**: servizio `api` fermo pulito il 26/09 19:06 UTC
   ("Stopping Container"), `railway up` rifiutato ("Your trial has expired"),
   deploy bloccati. NON un guasto tecnico: tutte le 40 env intatte (token len 46
   sha12 `0a4a7040dbc3` = vault), volume `api-volume` 355 MB intatto.
2) Il proprietario aveva ripuntato il deploy da un **NUOVO account Railway**
   (`peppe85palermo1@libero.it`, progetto **`creative-vibrancy`**, servizio
   **`betting_bot`**) collegato a `siryo009/betting_bot`: il servizio aveva SOLO
   le var di sistema RAILWAY_* (zero env app) → lo stesso identico pattern
   dell'orfano `valiant-liberation` del 12/09.
**Lezione applicata**: `railway list` PRIMA di inseguire i log — ma stavolta non
bastava: il CLI era autenticato sull'account SBAGLIATO. Il crash era su un
account che il CLI non vedeva. `railway whoami` + login browserless
(`railway login --browserless`, codice device su railway.com/activate) hanno
rivelato il secondo account.

**Risoluzione (tutto il 27/09, ~1h).**
1. **Backup env**: `railway variables --kv` dal vecchio servizio → 25 variabili
   salvate in `~/railway_env_backup_20260927.kv` (chmod 600, valori MAI in chat;
   copie /tmp distrutte con shred). Da lì in poi il file e' la fonte.
2. **Env ricreate sul servizio nuovo**: 25/25 via `railway variable set KEY
   --stdin --skip-deploys` (ciclo di lettura dal file kv; zero falliti).
3. **Volume creato**: `railway volume add -m /app/data` → `betting_bot-volume`
   (⚠️ CLI 5.62: opzioni `-s/-p/-e` PRIMA del sottocomando; il path con link
   attivo evita un panic interno di `volume add`).
4. **Deploy**: `railway up --detach --yes` → deployment `4277da38` SUCCESS
   (18:05 UTC). Le vecchie env (ODDS_API_KEY nuova inclusa) funzionano:
   the-odds-api 382→378 crediti, ZERO 401.
5. **Verifiche produzione**: `/api/health` **200** sul dominio nuovo
   **`https://bettingbot-production-2538.up.railway.app`** (creato con
   `railway domain`, target port auto: web_api 8080); bot scan vivo (36 partite
   analizzate, gate leghe attivo); moduli `smart_hedging`/`adaptive_weighting`
   importati OK; **getMe 200 `@Calcifrrbot`** (token valido, verificato senza
   esporlo).
6. **Webapp Vercel ripuntata** (CLI autenticato `siryochy`, progetto
   `quotaverace`, root dir `webapp/`): `NEXT_PUBLIC_API_BASE` e `BACKEND_URL`
   (proxy `/api/backend/*` in next.config.js) aggiornate al dominio nuovo →
   redeploy prod. Verificato: `/dashboard` 200, proxy `/api/backend/api/health`
   200 (la root 307->/dashboard e' il redirect Next della home, normale).
7. **IaC**: `.railway/railway.ts` source api+surebet → `github("siryo009/betting_bot")`,
   commit `8ba7778` pushato (piano `0 to add, 3 to change, 0 to destroy`).
   ⚠️ Ma l'IaC e il progetto `quotaverace` stanno sull'account VECCHIO: per
   governare il progetto `creative-vibrancy` (nuovo account) la IaC andrebbe
   ripullata lì (`railway config pull`) — la vecchia file resta come storico.

**⚠️ STATO POST-MIGRAZIONE (da sapere).**
- **Il DB e' NUOVO**: il volume `api-volume` (355 MB: ledger, 15k match_results,
  661 team_ratings, ensemble, caches) sta sul volume dell'account VECCHIO
  (scaduto). Il container nuovo riparte da zero (volume 34 MB al primo giro):
  ratings/ensemble/ML si ricostruiscono con le sync (API_FOOTBALL_KEY attiva,
  `football_hist` marcatori di sync ABSENTI nel DB nuovo → la prima sync
  storica riscarichera' le 35 leghe: pacing 6.5s gia' in codice) e il ledger
  riparte dai segnali di oggi. Export dai dati vecchi possibile SOLO se
  l'account vecchio viene riattivato (sessione CLI vecchia non preservata: un
  `railway login` browser basta; il progetto e i suoi volumi non sono cancellati).
- **Cron surebet RICREATO il 27/09 sera** (vedi sotto).
- **Chiavi SSH Railway**: `railway ssh` sul nuovo account chiede la
  registrazione — FATTA il 27/09 sera (vedi sotto).
- **Dominio webapp invariato** (`quotaverace.vercel.app`), dominio API NUOVO:
  il vecchio `api-production-dffd.up.railway.app` appartiene all'account
  scaduto (404). Se il proprietario riattiva l'account vecchio NON far girare
  DUE bot col medesimo token Telegram (conflitto getUpdates 409): spegnere il
  vecchio o usare token diversi.
- `/tmp/railway_env_backup.kv` distrutto; copia autorizzata: `~/railway_env_backup_20260927.kv`.

### Follow-up eseguiti (27/09/2026, sera): SSH + sync + cron surebet

**1) CHIAVE SSH nuovo account.** La vecchia `quotaverace-debug` e' legata
all'account vecchio (una chiave = un solo account Railway). Generata
`~/.ssh/id_ed25519_railway` (commento `betting-bot-debug`) e registrata con
`railway ssh keys add -k <fingerprint>` — il CLI legge le chiavi dall'SSH
AGENT: se l'agente ne ha due, offre la prima e ssh si autentica come l'account
SBAGLIATO; per il nuovo account usare un agente temporaneo con SOLA la chiave
nuova (`eval $(ssh-agent -s); ssh-add ~/.ssh/id_ed25519_railway` prima di
`railway ssh`). Verificato: SSH OK + moduli `smart_hedging`/`adaptive_weighting`
importati sul container.

**2) DB NUOVO = VUOTO + API-FOOTBALL DI NUOVO SOSPESO (blocco esterno).**
Stato al 27/09: `team_ratings` 0 (tabella assente), `match_results` 0,
`sync_state` assente; ledger di oggi: 31 matches, 70 predictions, 716
market_quotes (il multi-mercato ha gia' lavorato). Sync storica lanciata a mano
(`football_hist.py --seasons 2` in background sul container): **0 righe su
tutte le leghe** — `GET /status` e `/fixtures` rispondono
`{"access": "Your account is suspended..."}`: l'account API-Football
(peppe85palermo1? la chiave `fc8972c3a59e` del 12/09) e' stato sospeso di
nuovo, come gia' l'11/09. Il codice gestisce bene il caso: errore "access" →
`retry` → 3 tentativi → 0 righe, **ZERO marker `sync_state` scritti** → dopo la
riattivazione (dashboard api-football.com) la sync riparte pulita con
`python3 football_hist.py --seasons 2` (o attendere il job 08:30 UTC).
Intanto i rating arrivano SOLO da `compute_ratings()` sul settlement (job 4h +
serali): senza `match_results` la tabella non nasce finche' non si referta la
prima partita — il gate modello resta cieco (profilo neutro) e la catena
decide `review` per DATA_QUALITY_LOW: comportamento prudenziale voluto.

**3) CRON SUREBET RICREATO sul nuovo account (procedura IaC-native).**
`railway config pull --force` → il progetto `creative-vibrancy` ha ora il SUO
file `.railway/railway.ts` (servizio betting_bot + volume, env tutte
`preserve()`; l'IaC del progetto vecchio resta in git history). Aggiunto il
servizio `surebet` (`fn`, `Dockerfile.surebet`, cron `*/15`,
`restartPolicyType NEVER`, volume dedicato `surebet-volume` 100 MB region sfo —
il progetto non supporta volumi condivisi) + env SUREBET_* da codice + 3
segreti copiati da `betting_bot` via pipe stdin (ODDS_API_KEY,
QUOTAVERACE_BOT_TOKEN, ADMIN_CHAT_ID; valori mai in chat). `config plan`: **2
to add, 0 to change, 0 to destroy** → apply OK. Primo deploy `railway up -s
surebet` → SUCCESS; primo run reale 17:01 UTC: `16 match MLB | crediti residui
372` e run completato (exit pulito, chiave condivisa funzionante — fine dei
401 del vecchio cron).

**4) VERIFICA FINALE (27/09 sera).** health API 200, webapp 200, proxy
webapp→api 200, ciclo `auto_bet` pulito ogni 60s (`equity 33.55 USDC`,
0 candidati giocabili, 0 errori), cron surebet attivo.

### NUOVA CHIAVE API-FOOTBALL + SYNC STORICA COMPLETATA (27/09/2026, tarda sera)

**1) CHIAVE NUOVA, ACCOUNT NUOVO, VIVA.** Il proprietario ha creato un account
API-Football NUOVO e ha incollato la chiave in chat (regola 7: da considerare
compromessa — **DA RUOTARE** appena possibile, Opzione A: la imposta lui con
`railway variable set API_FOOTBALL_KEY --stdin` dal repo, l'agente verifica
solo l'impronta). Impostata su `betting_bot` via stdin (`set: true`), redeploy
automatico `bcda5716` SUCCESS. Verifica SENZA esporre: len 32, sha12
`71adc24a8010` (= vault), `/status` live dal container → account "GIUSEPPE",
piano **Free attivo fino al 27/09/2027**, 0/100 richieste del giorno.
Vault locale aggiornato con MERGE esplicito (load_vault → sostituzione voce →
riscrittura atomica chmod 600, 5/5 segreti preservati). Nessuna copia della
chiave in `.env`/`~/.env` (verificato).

**2) SYNC STORICA COMPLETATA AL PRIMO GIRO (risultato MIGLIORE del vecchio
DB).** `football_hist.py --seasons 2` in background sul container: **17.159
partite su 41 leghe** (vecchio volume: 15.192), **581 squadre con rating**
(`team_ratings`), **82/82 marker** (41 leghe × 2 stagioni 2023+2024), log finale
"✅ Totale: 17159 partite | Rating ricalcolati". Tutto entro il tetto di 100
richieste/giorno: il memo stagioni in-process (`_SYNC_STATE["first_year"]`)
ha fatto saltare 2026/2025 dopo la prima lega e il pacing 6,5s ha evitato il
rate limit per-minuto. Il gate modello NON e' piu' cieco: `team_names` +
581 rating → la copertura del modello sui segnali SX torna misurabile, e la
catena `decision` smette di mandare tutto in `review` per DATA_QUALITY_LOW.
Il job giornaliero 08:30 UTC mantiene le leghe da qui in poi (marker gia'
scritti, zero riscariche). Ensemble ML: si ritrainera' da solo (05:45 UTC +
boot) quando il ledger avra' righe chiuse sufficienti.

**3) ROTAZIONE CHIAVE COMPLETATA (27/09 sera).** La chiave esposta in chat
(`adc2…106a`) e' stata rigenerata dal proprietario dal dashboard api-football.com
e impostata LUI STESSO su Railway via `railway variable set --stdin` (mai in
chat, Opzione A della regola 7). Verifica dell'agente SENZA esporre: redeploy
automatico `b9fa335b` SUCCESS (20:15), len 32, sha12 `9b878cf1f004` (≠ la
vecchia `71adc24a8010`), `/status` live dal container → account "GIUSEPPE",
piano Free attivo, 82/100 richieste del giorno (la sync pomeridiana). Vault
locale allineato leggendo il valore da Railway via pipe ( Railway → stdin →
vault: mai stampato), 5/5 segreti preservati. La chiave esposta in chat e'
MORTA (la rigenerazione la invalida).

**4) VERIFICA JOB POST-SYNC (27/09 sera, 18:00 UTC).** Job al boot VERI (non
solo registrati): backup 17:47 integrity ok; retrain ensemble eseguito e
correttamente DISATTIVATO ("Dataset troppo piccolo 0 < 30" — il ledger nuovo
non ha ancora chiusure; si attivera' da solo). Job analisi 18:00 UTC: rotazione
quote rifrescata (crediti 366), SX scan 31 partite, multi_market 636 quote.
**Primi 4 segnali strong_value del nuovo DB — tutti OU in telemetria** (Liga MX
Under 3.5 @1.49 EV+14.3%, PL Under 4.5 @1.32 EV+12.3%, MLS Under 4.5 @1.34
EV+12.3%, PL Under 4 @1.43 EV+14.4%): 0 ordini perche' il gate di prontezza OU
(`ou_live_ready`, soglia 20 chiusure giocabili post-19/09 con ROI positivo)
e' IN PAUSA sul DB NUOVO e il suo contatore e' ripartito da zero — l'OU restara in
shadow finche' la telemetria non si ricostituisce su questo ledger (e'
il comportamento prudente voluto, NON un guasto; nessun segnale AH al momento).
Schema `matches` sul nuovo DB: colonne `id/home_team/away_team` (non
`match_id/home/away`). Nota orari: `history_sync_job` e' alle **06:30 UTC**
(08:30 ITA; run_daily hour=8 minute=30-IT_OFFSET) — domani saltera' tutto
(marker 82/82 gia' scritti, zero richieste).

### Recinto di capitale in LIVE + suite verde al 100% (28/09/2026)

**Direttiva del proprietario**: blindare la gestione del rischio in LIVE con un
tetto inviolabile `min(stake, 1.50)` e portare la suite a verde prima del
rilascio. Commit `c2d38d7`, deploy Railway `e907eb81` SUCCESS.

**1) Il recinto (tre guardie, `auto_bet.py`).**
- `cap_order_stake()` = **`min(stake, 1.50)` HARDCODED**: tetto assoluto per
  SINGOLO ordine reale, NON disattivabile via env (`ORDER_MAX_STAKE_USDC=0.0`
  non lo spegne — e' la regola di business, difesa da
  `TestMicroStake::test_tetto_inviolabile`). Riduce, non alza mai.
- `OPEN_EXPOSURE_CAP_PCT` = **0.40**: quando l'esposizione APERTA (stake delle
  bet `mode='live'` non ancora saldate = capitale immobilizzato) raggiunge il
  40% del bankroll il giro DEGRADA a shadow (nessun nuovo ordine reale,
  valutazione e telemetria proseguono; si sblocca da sola coi settlement).
  Complementare a `TOTAL_EXPOSURE_CAP_PCT` (flussi del giorno). Lettura fallita
  -> `inf` (fail-closed: il recinto non si apre per un errore di lettura).
- `CHIEF_EXECUTION` (default `off`): con `live` i piani approvati dalla catena
  piramidale entrano nella STESSA coda di esecuzione della corsia storica
  (T-60, liquidita', oracolo top-down, cap, dedup) — nessun canale parallelo.

**2) I due test rotti del recinto (difetti del TEST, cap non toccato).**
- `test_puntata_saldata_libera_il_capitale` chiamava
  `tracker.save_result("sx-1", 2, 1)`: firma SBAGLIATA (reale `match_id,
  league, home, away, sh, sa, settled_at`) -> `TypeError`.
- `test_esposizione_piena_nessun_ordine_reale` usava
  `_stub_wallet(33.55, exposure=13.42)`, ma il PRIMO argomento e' il
  DISPONIBILE: l'equity diventava 46.97 (cap 18.79) e 13.50 di esposizione non
  bloccava nulla. Corretto in `33.55 - 13.42` (equity 33.55, cap 13.42).

**3) Il tetto non era isolato negli altri test (`conftest.py`).** Il conftest
lo "disattivava" con `ORDER_MAX_STAKE_USDC = 0.0`, ma la funzione e' hardcoded
-> il tetto si applicava a TUTTA la suite e rompeva i test di staking
(`test_auto_bet_live`: stake attesi 3.0/3.98, ottenuti 1.50). Ora l'isolamento
sostituisce la FUNZIONE (`cap_order_stake`) per tutti i file TRANNE
`test_capital_enclosure.py` (nodeid escluso), che esercita quella VERA: 21 test
verdi. Il valore di produzione non e' mai stato modificato.

**4) 4 rossi PREESISTENTI: env perse dal `config pull` del 27/09.** La
rigenerazione dell'IaC sul nuovo account aveva perso le dichiarazioni
`preserve()`, e i tripwire `test_*_env_dichiarate_nella_iac` le difendono:
fallivano adaptive weighting, smart hedging, top-down e book flow. Ripristinate
in `.railway/railway.ts` (~30 env, incluse le nuove del recinto
`OPEN_EXPOSURE_CAP_PCT`, `ORDER_MAX_STAKE_USDC`, `CHIEF_EXECUTION`). `preserve()`
NON crea valori: le variabili assenti restano assenti (valgono i default di
codice) e `config apply` resta "0 to destroy".

**5) Esito.** Suite **2686 test su 102 file, 0 fallimenti** (`-m "not
integration"`, offline; i 2 `integration` restano opt-in su rete/chiavi). Push
su `main` -> deploy automatico **SUCCESS**, health **200**. Log del giro
`auto_bet` (00:05 UTC): `esposizione aperta 0.00/13.42 USDC (cap 40%, tetto
per-ordine 1.50 USDC)` — recinto attivo in produzione; 1 pick AH saltato da
`no_oracle` (gate top-down fail-closed), 0 ordini come atteso.

### Risk management: stake FISSO 1.50 + recinto 40% dinamico fra Execution e Advisor (28/09/2026)

**Direttiva del proprietario**: la size di ogni singola scommessa REALE e'
esattamente **1.50 USDC**; il bot non supera MAI il **40% del bankroll
impegnato simultaneamente** (8 ordini aperti = 12.00 USDC); il limite **non e'
giornaliero** ma basato sugli **ordini in corso**; chiuso un match il bot
riparte subito col **nuovo 40% del capitale aggiornato**. Da implementare: la
**lettura dello stato degli ordini aperti fra Execution Engine e Advisor**.

**1) STAKE FISSO (`auto_bet.py`)** — `ORDER_FIXED_STAKE_USDC` (default **1.50**),
`fixed_order_stake()`, `fixed_stake_active()`, **`order_stake(stake,
spendable)`** = UNICO punto di verita' dello stake degli ordini reali:
- importo FISSO, non piu' Kelly; l'env puo' solo ABBASSARLO (il tetto
  per-ordine 1.50 resta inviolabile);
- **vincolo di cassa**: con meno di 1.50 USDC liberi lo stake e' 0 e l'ordine
  viene SALTATO (fail-closed) — mai un importo diverso dalla direttiva per far
  passare un ordine;
- `ORDER_FIXED_STAKE_USDC=0` ripristina lo staking dinamico storico (usato da
  test e diagnostica);
- applicato in TUTTE le corsie (storica, multi-mercato, chief). **SIM
  invariata**: la cassa simulata alimenta ML/CLV e non si cambia d'era.
- ⚠️ I cap di portafoglio (correlazione 30% / esposizione 40%) decidono **SE**
  (un candidato ridotto sotto 1.50 viene saltato, non rialzato); l'importo
  fisso decide **QUANTO**.

**2) RECINTO DI ESPOSIZIONE APERTA — lettura Execution EngiNE -> Advisor.**
- `auto_bet._open_live_snapshot()` -> (stake in gioco, numero ordini) da `bets`
  `WHERE esito_finale IS NULL AND mode='live'`: sono gli ORDINI IN CORSO, quindi
  il **rilascio e' dinamico per costruzione** (chiuso il match, la riga esce dal
  conteggio — nessuna finestra giornaliera da riarmare).
- **`auto_bet.exposure_allows(bankroll, new_stake)`** = la PROIEZIONE (aperto +
  nuovo stake <= 40% del bankroll). Con equity **33.55** (cap **13.42**) e stake
  1.50 entrano **8 ordini (12.00)**; il nono sarebbe **13.50** -> respinto.
  Fail-closed su lettura impossibile o bankroll non positivo.
- **`ExecutionAgent.open_exposure(bankroll, new_stake)`** (`agents/execution_agent.py`):
  l'Execution Engine e' l'unico che sa cosa e' stato eseguito, quindi e' lui la
  FONTE dello stato; delega a `auto_bet.exposure_allows` con import PIGRO
  (`import agents` resta leggero) e NON reimplementa la soglia.
- **`AdvisorAgent.exposure_status()` / `exposure_gate()`**
  (`agents/advisor_agent.py`): il Capo gli passa il bound method dell'Execution
  Agent (`advisor.exposure_reader = execution.open_exposure`, wiring in
  `chief_orchestrator`), e l'Advisor **interroga lo stato a OGNI ciclo**:
  `resolved=False` + `original_reason="exposure_cap"` quando la proiezione
  sfora. Il tetto e' sempre `equity x 40%` **letto fresco** -> compounding
  automatico e ripresa senza interventi.
- **Ciclo del Capo (nuovo step 3c)**: `report.exposure` registra lo stato e i
  piani approvati vengono filtrati dal gate PRIMA dell'esecuzione (gate non
  leggibile = RESPINTO). Il **micro-stake NON si applica**: un tetto di capitale
  non si negozia col ridimensionamento. Il **kill switch resta la prima
  autorita'** (l'esposizione non lo maschera).
- La corsia di DENARO (dal 27/09) ora usa la **proiezione per candidato** e non
  la sola soglia di blocco: piu' ordini nella stessa tornata non sommano oltre
  il 40%.

**3) OSSERVABILITA'**: `CycleReport.exposure` + `as_json()` che ora espone anche
`advisor` (prima il campo non era serializzato: `advisor_kinds` era vuoto **per
costruzione**); `chief_shadow_wiring.summarize()` conta `exposure_blocked`
(cicli al tetto) e `exposure_gates` (piani respinti) e `format_report()` stampa
`Esposizione aperta: X/Y USDC (N ordini aperti, … cicli al tetto, … piani
respinti)`.

**4) TRIPWIRE**: **`test_exposure_gate.py`** (nuovo, 27 test: parita' con il
tetto di `auto_bet`, proiezione 8/9 ordini con equity 33.55, rilascio dopo il
settlement, compounding sull'equity raddoppiata, fail-closed su lettore rotto,
kill switch prima autorita', filtro dei piani approvati nel ciclo, nessuna
soglia duplicata negli agenti, env dichiarate in IaC) + **`TestFixedStake`** in
`test_capital_enclosure.py` (importo esatto, env che abbassa, `0` = dinamico,
fondi insufficienti = 0, ordine reale esattamente 1.50, SIM invariata) + il
`test_il_nono_ordine_non_entra_per_proiezione` nel giro reale.
`conftest.py` isola lo stake fisso (i test misurano altro: cap, wallet,
liquidita', stop-loss) e il **lettore di default del recinto** (senza
isolamento la lettura reale aprirebbe il DB di produzione nei test che non lo
patchano); `test_auto_bet_live.py` ha un fixture autouse che dichiara
l'isolamento del recinto (wallet 3 USDC: cap 1.20 respingerebbe ogni ordine).

**5) `verify_guardrails.py` — A–H TUTTI BLOCCANO (exit 0)**:
- **C** riscritto ("stake fisso 1.50 + cap severo"): 1 ordine da **esattamente
  1.50**; fondi liberi 1.20 -> **0 ordini** (fail-closed); staking dinamico
  (`FIXED=0`) + cap severo -> 0 ordini; controprova `STAKE_CAP_HARD=0` -> floor
  1 USDC accettato.
- **H** NUOVO: 8 ordini aperti (12.00/**13.42**) -> l'Advisor respinge il nuovo
  stake e il giro non manda nulla al provider.
- **D** riparato (era rosso dal 26/09): lo scenario accende ESPLICITAMENTE
  `TOP_DOWN_BYPASS` nella sua parte LIVE e verifica a parte che il **default
  resti spento**; l'attesa sui candidati include ora anche `g-valid` (la corsia
  top-down prende ogni riga 1X2 e lascia decidere l'oracolo; `g-legacy` resta
  fuori per LEGA: il bypass e' di prezzo, non di strategia).
- `_reset_state()` azzera anche il **CB settimanale**: la diagnostica fa
  oscillare il bankroll fra scenari e un salto veniva letto come drawdown
  (-96.8%) armando il blocco per gli scenari successivi.

**6) Env**: `ORDER_FIXED_STAKE_USDC` dichiarata `preserve()` in
`.railway/railway.ts` (accanto a `OPEN_EXPOSURE_CAP_PCT` e
`ORDER_MAX_STAKE_USDC`). ⚠️ **NON impostate su Railway**: valgono i default di
codice (fisso **1.50**, cap **40%**).

**7) Verifica**: **252 test verdi** nel lotto mirato (exposure_gate,
capital_enclosure, auto_bet_live, auto_bet, agent_hierarchy, advisor_agent,
chief_shadow_wiring, risk_guards, top_down, railway_drift_check) + **tutti i
lotti della suite** (`-m "not integration"`, 11 lotti, 103 file) verdi,
`compileall` OK, 0 marker di conflitto.

### Significativita' statistica + dipendenze dichiarate (28/09/2026)

**Direttiva del proprietario**: aggiungere la libreria di analisi statistica
(`scipy`, `numpy`, `pandas`, `aiohttp`, `requests`, `pydantic`) e usarla per
rendere leggibile il campione del ledger. Il modulo di significativita' e' la
risposta al problema che ha gia' prodotto due letture sbagliate in una
settimana (22/09: `-21,64%` sul 1X2 letto come prova contro la strategia, ma
74 delle 85 righe erano pre-11/09; 25/09: `+21,21%` OU portato per intero da
22 righe pre-19/09).

**1) DIPENDENZE — cosa c'era davvero (audit, non assunzioni).**

| libreria | stato PRIMA | azione |
|---|---|---|
| `numpy` 2.5.2 | in uso (ml_ensemble, probability_calibration) ma **solo transitiva** | dichiarata esplicitamente |
| `scipy` 1.18.1 | **installata e MAI importata** (arriva con scikit-learn) | dichiarata + USATA (questo modulo) |
| `pandas` 3.0.5 | in uso (odds_ingest, surebet_scanner) | gia' dichiarata |
| `requests` 2.34.2 | in uso in 10+ moduli | gia' dichiarata |
| `pydantic` 2.13.5 | in uso in `decision/` e `research_graph/` | gia' dichiarata |
| `aiohttp` | assente | **installata (3.14.3) e dichiarata** |
| `statsmodels` | assente | **NON aggiunta** (scelta del proprietario): `scipy.stats` copre i test e `sklearn`/`xgboost` le regressioni — sarebbe peso morto |

⚠️ Nota onesta registrata: `aiohttp` **non e' ancora utilizzata da nessun
percorso**. Il collo di bottiglia delle API non e' la latenza ma il **budget
crediti** (500/mese) e il **rate limit** (10 req/min su API-Football, pacing
6,5s gia' in `football_hist._throttle`): un fetch asincrono consumerebbe i
crediti piu' in fretta, non produrrebbe piu' edge. Il parallelismo dove serve
esiste gia' (`sx_signals._books_parallel`). La dipendenza resta dichiarata
(richiesta diretta) e pronta per un client asincrono quando servira'.
**Effetto pratico**: `requirements.txt` non dipende piu' da arrivi TRANSITIVI —
un cambio di versione a monte non puo' piu' cambiare il comportamento del
progetto senza che nessuno lo veda.

**2) `significance.py` (nuovo, top-level come `flow_measure.py`) — sola
LETTURA, offline, zero crediti.** Non decide nulla: dice se il risultato e'
**distinguibile da zero** e **quante chiusure servono**.
- `wilson_interval` (regge agli estremi: 0 su 8, 8 su 8, dove l'intervallo
  normale produce limiti fuori da [0,1]), `hit_rate_test` (binomtest esatto),
  `roi_test` (t-test sul P/L per unita' di stake, **stessa definizione di ROI
  del report**: media su TUTTE le chiuse, push a 0), `required_n`,
  `detectable_edge` (l'INVERSO di `required_n`), `breakeven_hit_rate`.
- `evaluate(rows)` -> blocco con campione, ROI + **CI95**, p-value, hit rate +
  CI95, confronto con la **prob. del modello** (overconfidence) e con la
  **break-even implicita dalla quota media**, edge minimo rilevabile, chiusure
  necessarie e `status` machine-readable (`insufficient` / `no_edge` /
  `positive` / `negative` / `unavailable`).
- `from_ledger(...)` legge il ledger con i filtri CONDIVISI
  (`tracker.filter_predictions`: era + fascia quota) e `by_market`.
- CLI: `venv/bin/python significance.py [--json] [--since D] [--odds-min X]
  [--odds-max Y] [--all-statuses] [--db PATH]`.

**3) IL NUMERO CHE RENDE ONESTI I GATE DI DECISIONE.** A quota media 1.65
(size al floor del progetto, quota media reale della fascia 1.30-1.80):

| chiusure | edge minimo distinguibile (potenza 80%) |
|---|---|
| **30** | **41,2%** |
| 100 | 22,6% |
| 500 | 10,1% |

E per confermare un edge del **+2%** servono **~12.700 chiusure**. Conseguenza
dichiarata: la soglia "30-40 chiusure dell'era nuova" **non puo' confermare un
edge del +2%** — puo' solo **smentire un disastro** (ROI fortemente negativo)
o confermare un edge enorme. E' la stessa conclusione del 22/09 arrivata per
via statistica invece che per intuizione: prima di leggere un ROI come misura,
guardare l'intervallo. Due tripwire bloccano la cosa
(`test_30_chiusure_non_possono_confermare_un_edge_del_2pct`).

**4) INTEGRAZIONE (nessuna soglia duplicata, nessun giudizio cambiato).**
- `multi_market.shadow_report()` -> `markets[mt]["significance"]` (popolazione
  **giocabile**: le righe conservate sono le stesse che alimentano il bucket) +
  righe nel `format_report` sotto la riga "giocabili".
- `market_diagnose.analyze_db()` -> `significance` (totale) + per mercato, con
  sezione "🧮 Significativita' del campione" nel report. **I giudizi non
  cambiano**: un campione sotto la soglia resta `insufficient` anche col
  p-value piccolo (verificato da un test).
- **`GET /api/significance`** (nuovo endpoint): `?since=`, `?odds_min=`,
  `?odds_max=`, `?all=1` (modalita' confronto su tutto il ledger). Include il
  report testuale in `report`.
- `significance.MIN_SAMPLES` == `multi_market.MIN_RELIABLE_CLOSED` == 30
  (tripwire: un progetto, una sola idea di "campione affidabile").
- **Degrado, non crash**: senza `scipy` il modulo risponde `unavailable` e i
  report stampano una riga di degrado; un errore di lettura diventa un blocco
  con il motivo. In ogni punto l'errore e' **inghiottito e contato**, mai
  propagato (la diagnostica non deve poter rompere un report).

**5) GARANZIE (tripwire in `test_significance.py`)**: nessuna istruzione di
scrittura nel sorgente, connessione SEMPRE `mode=ro` (il test tenta una
`UPDATE` e pretende che SQLite la rifiuti), nessuna rete (niente `requests`/
`aiohttp`/`odds_api`/`sx_signals`), nessun ordine (`place_limit_order`,
`execution_engine`, `_live_fill`, `save_bet`), e `import significance` in
sottoprocesso NON carica `tracker`/`auto_bet`/`bot`/`odds_api`/`decision`.

**6) Verifica**: **`test_significance.py` = 63 verdi, tutti OFFLINE** (ledger
temporaneo, nessuna rete) + 3 lotti di regressione (871 test:
multi_market/market_diagnose/predictions/web_api/reports/performance/flow;
league_gate_impact/value_filter/market_calib/risk_guards/auto_bet x2/
capital_enclosure/exposure_gate/advisor/hierarchy/t60/ou; bot/decision x5/
chief/secret_hygiene/settlement/liquidity). `verify_guardrails.py`: **A–H
tutti bloccano**. `compileall` OK, 0 marker di conflitto.

### Verifica degli ordini reali: `order_watch.py` (28/09/2026)

**Perche' esiste**: gli invarianti del 28/09 (**stake fisso 1.50 USDC**,
**recinto 40%** = 13.42 a equity 33.55 → **8 ordini**) sono invarianti sul
DENARO. Verificarli a mano una volta non serve: un ordine fuori regola puo'
arrivare in qualsiasi momento e senza un controllo che si RIPETE nessuno se ne
accorgerebbe fino al drawdown. Primo ordine reale atteso in questa fase:
**0** — il ledger `bets` della nuova infrastruttura e' **vuoto** (0 righe dal
reset del 27/09) e i candidati giocabili sono 0: la corsia e' armata e valuta,
ma non c'e' niente da giocare.

**Cosa controlla** (`audit()` → `violations` + `declared` + `max_open`):
1. **stake esatto** — ogni riga `mode='live'` creata dalla data della direttiva
   deve avere `stake == auto_bet.fixed_order_stake()` (valore letto, mai
   copiato). Le righe **precedenti** sono dichiarate `stake_predirective`, non
   giudicate: applicare la regola di oggi al passato sarebbe un falso positivo
   su una strategia diversa.
2. **tetto per-ordine inviolabile** (`ORDER_MAX_STAKE_USDC`) — vale anche sulle
   righe pre-direttiva e anche con lo stake fisso spento: non e' una regola di
   strategia.
3. **recinto 40%** — replay CRONOLOGICO di aperture e chiusure: a ogni apertura
   la somma degli stake aperti deve stare dentro `equity_istante x 40%`.
   L'equity dell'istante viene dallo storico campionato
   (`bankroll_history.json`, 1 campione/ora): `source` dichiara la provenienza
   (`sample` / `nearest` / `now`), perche' **un tetto STIMATO non e' un tetto
   verificato** — senza campione la riga finisce in `declared`
   (`cap_estimated` / `cap_unverifiable`), mai in "ok".
   A parita' di istante le CHIUSURE precedono le aperture (altrimenti una
   sostituzione nello stesso secondo conterebbe come sfondamento).
4. **ordine senza `bet_id`** su `FULLY_FILLED` (guardia del 26/09).

**Verificato in locale su ledger temporaneo**: 8 ordini da 1.50 = **12.00**
dentro il tetto **13.42** (picco ricostruito, fonte `sample`); il **nono**
(13.50) viola; con il **settlement** del primo il nono **entra** (rilascio
dinamico); a equity 100 il tetto diventa 40.00 e ci stanno 26 ordini
(compounding).

**In produzione**: job **`bot.order_watch_job` ogni 30'** (piu' frequente degli
altri monitor: un ordine fuori regola va visto adesso, non fra 6 ore) — logga
SEMPRE lo stato e allerta admin+iscritti SOLO sulle violazioni, anti-spam
1/giorno (chiave `ORDER_WATCH`). Comando admin **`/ordini`**. CLI:
`venv/bin/python order_watch.py [--json] [--db PATH] [--equity N]` e la
**sorveglianza** `--wait 15 --interval 60` (verifica ogni nuovo ordine appena
compare, exit 1 se viola). Sola lettura (`mode=ro`), zero crediti, zero ordini.

**Isolamento test**: `conftest.py` isolava `cap_order_stake`/`FIXED_STAKE_USDC`
per tutti i file tranne `test_capital_enclosure`; ora l'eccezione e' un elenco
DICHIARATO (`test_capital_enclosure`, `test_order_watch`) — sono i due file che
esercitano il recinto VERO, entrambi su ledger temporanei.

**Test**: `test_order_watch.py` **33 verdi, tutti OFFLINE** (stake esatto,
pre-direttiva, tetto che vale con stake fisso spento, 8/9 ordini, rilascio al
settlement, compounding a equity 100, tetto stimato dichiarato, equity
iniettabile, report, CLI exit code, sorveglianza che rileva il primo ordine,
job e comando registrati, job silenzioso senza violazioni e che allerta con
esse, job che non esplode su ledger rotto, sola-lettura/nessuna rete/nessun
ordine, nessuna soglia duplicata). Lotti di regressione verdi: bot + auto_bet
x2 + capital_enclosure + exposure_gate + order_watch; t60 + risk_guards +
advisor + hierarchy + chief + weekly; significance + multi_market +
market_diagnose + predictions + web_api + reports; secret_hygiene +
liquidity + top_down + hedging + book_flow. `verify_guardrails.py`: **A–H
tutti bloccano**. `compileall` OK, 0 marker.

### Intel live nel Data Agent + suite sbloccata (29/09/2026)

**Direttiva**: dare al Data Agent un FOCUS INTEL a costo zero (statistiche di
stagione, ELO, news infortuni, lanciatori MLB, statistiche NBA) per i match in
finestra — senza API key e senza toccare il denaro.

**1) `live_intel.py` (nuovo, 693 righe) — raccoglie, non decide.**
Cinque provider, tutti gratuiti e con import PIGRI:
| provider | libreria | cosa porta |
|---|---|---|
| statistiche calcio | `soccerdata` → FBref | xG/xGA, gol fatti/subiti, partite |
| ELO | `soccerdata` → ClubElo | rating ELO (club europei) |
| news | `ddgs` (DuckDuckGo) | titoli infortuni/formazioni |
| MLB | MLB StatsAPI pubblico (`requests`) | probabili lanciatori del giorno |
| NBA | `nba_api` | punti, OPP_PTS, GP, win% |
Contratti Pydantic (`MatchIntel`/`TeamStats`/`NewsItem`/`ProviderStatus`),
`as_json()` per log/HTTP. Titoli e link delle news, MAI il corpo: sono un
PUNTATORE alla fonte.

**2) Cablaggio (`agents/data_agent.py`, `agents/contracts.py`).** Il Data
Agent raccoglie l'intel per i match dei segnali (una voce per MATCH, mai per
segnale) e la mette in `MarketData.intel` (`CycleReport.market` la include).
I nomi delle squadre vengono dalla tabella `matches` (il `Signal` non li porta):
senza riga `matches` NESSUNA intel — mai costruita su match_id opachi.
**L'intel NON e' un gate**: vuota o con provider in errore il ciclo prosegue
(degrado dichiarato in `providers[].detail` + `errors`/`partial`),
disattivabile con `LIVE_INTEL=0`. L'Advisor riceve gia' il `MarketData` in
`resolve_blocker(..., data_report, ...)`: il consumo e' il passo naturale
successivo, la raccolta e' completa.

**3) IL BLOCCO DELLA SUITE (causa trovata e chiusa).** `DataAgent().process()`
con segnali e nessun `intel_fn` iniettato chiamava gli scraper REALI:
`test_agent_hierarchy`/`test_advisor_agent`/`test_exposure_gate` costruiscono
`DataAgent()` "nudo" e facevano partire lo scraping FBref a OGNI test —
`test_exposure_gate` 48s e (senza la guardia) attese senza timeout. Due
contromisure: **`conftest.py`** spegne l'intel alla fonte (`LIVE_INTEL=0`) e
porta la cache nella tmp; **`pytest.ini`** aggiunge `-p no:seleniumbase`
(SeleniumBase non e' una dipendenza del progetto ma, se presente, inietta il
proprio plugin in ogni run: banner, download di webdriver in
`downloaded_files/`, lock all'avvio — osservato il 29/09). `-p no:` non
solleva se il pacchetto manca.

**4) Guardia di TIMEOUT (`_network_deadline`).** `soccerdata` e `ddgs` non
espongono un timeout: senza guardia una fonte lenta blocca il ciclo a tempo
INDEFINITO. Il modulo limita la durata agendo sul default dei socket
(il punto che urllib3/requests rispettano quando il chiamante non passa un
timeout) e RIPRISTINA sempre il valore precedente — il processo ospita anche
lo scheduler del bot. `LIVE_INTEL_TIMEOUT_S` (default 20s); un valore non
numerico o <= 0 ricade sul default: una guardia non si spegne con un env
sbagliato.

**5) Cache su disco con TTL** (`LIVE_INTEL_CACHE`, default `DATA_DIR/intel`;
`LIVE_INTEL_TTL_<PROVIDER>`): le librerie sono gratuite ma NON a costo zero in
TEMPO, quindi il ciclo non martella le fonti. `data/intel/` e' gitignored.

**6) Fix `backtest_mc.py` — era O(n²).** `_walk_forward` riaddestrava
l'ensemble AD OGNI RIGA sullo STESSO campione (train deterministico del
medesimo slice): ora il modello dell'epoca si conserva e si riusa. Misurato:
il file di test (7 casi) e' passato da 2:47 a **1m48** e la regressione non
sfora piu' il budget della shell sembrando bloccata.

**7) Dipendenze dichiarate** (`requirements.txt`, tutte PIGRE): `soccerdata`
1.9.1, `nba_api` 1.11.4, `pybaseball` 2.2.7, `ddgs` 9.16.0 — installate e
verificate. ⚠️ `pybaseball` e' dichiarata ma la versione 2.2.7 **non espone
`probable_starters`** e non e' invocabile fuori dal baseball: l'adapter usa
l'endpoint pubblico MLB StatsAPI e `pybaseball` resta come dipendenza
richiesta (nessun numero inventato).

**8) IaC.** `LIVE_INTEL`, `LIVE_INTEL_CACHE`, `LIVE_INTEL_TIMEOUT_S` e i cinque
`LIVE_INTEL_TTL_*` dichiarati `preserve()` in `.railway/railway.ts` (senza,
un `config apply` distrugge cio' che l'operatore imposta). Tripwire
`TestIaC` in `test_live_intel.py`: env dichiarate, **ogni env letta dal modulo
presente nella IaC** (i TTL sono composti a runtime: i provider si ricavano
dalle chiamate `_cache_*`), e le 4 dipendenze presenti nei requirements.
`railway config plan` verificato dopo la modifica: **"already up to date"**
(0 to add, 0 to change, 0 to destroy) — nessuna variabile da distruggere.

**9) Test**: `test_live_intel.py` **50 verdi, tutti OFFLINE** (provider finti
in `sys.modules` o iniettati, cache nella tmp, DB temporaneo): contratti,
mappa leghe, query news, cache (scaduta/corrotta), guardie di rete (timeout
attivo DURANTE la chiamata, ripristinato dopo, env impossibili), assembler
(soccer/nba/mlb, provider rotto che non nega gli altri), tripwire
(nessun import di produzione, import leggero in sottoprocesso, nessuna soglia
di strategia nel sorgente) e integrazione `DataAgent` (intel per match, una
sola voce con piu' segnali, fail-safe, switch `LIVE_INTEL`, gate intatto).
Regressioni verdi: gerarchia+advisor+recinto+chief (142),
`backtest_mc` (7), `railway_drift_check`+`adaptive_weighting`+`smart_hedging`
(121). `compileall` OK, 0 marker di conflitto.

**10) STATO: non ancora committato.** Il lavoro e' nel working tree
(`live_intel.py` e `test_live_intel.py` sono untracked; `agents/data_agent.py`,
`agents/contracts.py`, `conftest.py`, `pytest.ini`, `backtest_mc.py`,
`requirements.txt`, `.gitignore`, `.railway/railway.ts` modificati). In
PRODUZIONE vale ancora il codice precedente: l'intel NON gira sul container.
Da fare prima del push: `compileall`, i lotti di regressione, `railway config
plan` (**0 to destroy**), poi il deploy — e dopo, la prima esecuzione reale
(`venv/bin/python live_intel.py "<casa>" "<trasferta>" --league "..."`) per
misurare quali provider rispondono DAVVERO dalle rete/container: FBref e DDG
possono essere bloccati e il fail-safe li rende indistinguibili da "nessun
dato" (per questo ogni provider dichiara il proprio `detail`).
⚠️ Nota di perf (NON risolta, pre-esistente e non causata da questo lavoro):
il fixture autouse di `conftest.py` costa ~2,5s di `setup` per test
(misurato anche su file vecchi come `test_tier.py`): e' il costo di import +
`config` (vault Fernet/PBKDF2) per ogni test, non un'attesa di rete.

### Quattro direttive tecniche del 29/09 + oracolo eSports (OddsPapi)

Direttive del proprietario: (1) networking asincrono, (2) motore statistico,
(3) conferma web3/eth-account per EIP-712, (4) tipizzazione finanziaria
(Divimal ai confini, divieto float sul denaro) + item aggiuntivo: **eSports
con provider odds esterno** come oracolo per la corsia top-down. Scelte
confermate via `ask_user`: realtime ADDITIVO (client async in modulo nuovo,
pipeline sync intatta), vettorizzazione DENTRO `poisson_engine.py` (fonte
unica: `agents/strategy_agent.py` DELEGA), Decimal SOLO AI CONFINI (contratti
+ conversione agli estremi, aritmetica interna invariata), eSports con
**OddsPapi** (scelto sui fatti: il free tier INCLUDE Pinnacle su eSports,
~4.5% margine mediano Match Winner; scartato odds-api.io = solo 2 bookmaker
ricreativi nel free tier, nessuno sharp → oracolo inutile).

**1) NETWORKING ASINCRONO — `sx_realtime.py` (nuovo, 54 test verdi).**
Client async Centrifugo per SX Bet (`wss://realtime.sx.bet/connection/
websocket`), token richiesto a `/user/realtime-token-v3/api-key`
(`REALTIME_ENDPOINT_PATH` — ⚠️ il nome iniziale `TOKEN_PATH` faceva SCOPPIARE
`test_secret_hygiene`: nome credential-like + valore simile a chiave; rinominato
e test aggiornato). Fail-closed senza `SX_API_KEY`. Trasporto (`http_async`)
iniettabile → test interamente OFFLINE. Dipendenze dichiarate in
`requirements.txt` (prima solo transitivi): `httpx>=0.27`, `websockets>=13`.
Nessun collegamento alla pipeline: il realtime e' un canale AGGIUNTIVO.

**2) MOTORE STATISTICO — Poisson vettorizzato.** `poisson_engine.py`
riscritto internamente con numpy (matrici punteggio), Dixon-Coles e rating
time-decay PRESERVATI; parita' col vecchio percorso verificata da
`test_poisson_vectorized.py` (~1500 casi). `agents/strategy_agent.py` resta in
delegra (nessuna formula copiata, regola anti-bug del 13/09 e 27/09).

**3) WEB3/ETH-ACCOUNT — confermato.** `eth-account` 0.14.0 installato e USATO
(`execution_engine._sign_order` via `encode_typed_data`+`sign_message`, 26 test
verdi). **web3 NON installato e NON necessario**: l'ordine e' firmato EIP-712
lato client e inviato via REST; aggiungere web3 sarebbe peso morto. Non
aggiunto.

**4) TIPIZZAZIONE FINANZIARIA — Decimal ai confini (fix BUG VERO incluso).**
`decision/models.py`: `money()` (costruzione via STRINGA, mai da float),
`as_float()` (unico punto di uscita), `Money = Annotated[Decimal, ...]` con
serializzazione JSON → float; `validate_assignment` su `Signal`/`StakeDecision`;
campi denaro (bankroll/stake/kelly_stake/floor/price) in Money,
kelly_fraction/cap_pct restano float (NON sono denaro). Esteso a commands/
stake_engine/pipeline/shadow/engine/review_queue/review_telegram/middleware
(`_json_default` Decimal→float) e `agents/finance_agent.py` (docstring DIRETTIVA
FINANZIARIA, ingresso `money()`, unico `as_float` verso `build_plan`).
**BUG VERO trovato dal confine tipizzato**: `decision/risk_engine.py`
confrontava `signal.price` (Decimal) direttamente con soglie float →
`Decimal("1.3") < 1.30` era **TRUE** (il float 1.30 vale 1.30000000000000004):
un segnale ESATTO a 1.30 veniva rifiutato al confine della fascia. Fix:
`price = as_float(signal.price)` UNA volta prima dei gate + tripwire
(`test_money_decimal.test_il_confronto_grezzo_col_float_e_una_trappola`, che
replica la trappola con `pytest.approx`). `test_money_decimal.py` ~40 test
verdi (tripwire ast-based con `_code_only`: le docstring NOMINANO float(/Decimal(
per spiegare la direttiva, il tripwire non deve colpire la PROSA).
Residuo accettato: `agents/advisor_agent.py` ~riga 250 fa `float(...)` per
telemetria (transito al confine, non aritmetica) — eventuale cleanup futuro.

**5) ESPORTS — fatti MISURATI (29/09, probe reali, zero crediti) e oracolo.**
- **SX Bet**: eSports = `sportId=9`, 67 mercati: **type 52** = moneyline 2 vie
  SENZA pareggio (23), **type 3** = AH con linea (23), **type 1536** = O/U
  totale mappe (21); `liveEnabled: false` su tutti → pre-match.
- **the-odds-api NON ha NESSUN gruppo eSports** (lista ufficiale /v4/sports
  letta per intero): senza oracolo esterno il gate top-down risponde
  `no_oracle` → **nessun ordine eSports possibile** (fail-closed per progetto).
- **OddsPapi** (provider scelto): BASE `https://api.oddspapi.io/v4`, auth via
  QUERY PARAM `apiKey` (nessun header → niente chiavi nei log header);
  sportId: dota2=16, cs2=17, lol=18, cod=56, rocket_league=59, valorant=61;
  **`/v4/account` NON consuma quota** ed e' sempre accessibile (probe di
  salute); 429 = `REQUEST_LIMIT_EXCEEDED`.
  ⚠️ **DUE ASSERZIONI DOCUMENTALI SMENTITE DAI DATI LIVE (29/09, container,
  fixture reali CBLOL/LCS)**: (1) il parametro `bookmakers=pinnacle` FA
  SVUOTARE `bookmakerOdds` (payload ridotto ai soli metadati, 12 chiavi);
  SENZA il parametro il payload porta TUTTI i book (89) — il filtro book va
  fatto LATO CLIENT, la query NON invia piu' `bookmakers` (tripwire);
  (2) il Match Winner compare col market id **185** ed esiti **185/186**, non
  171/172 come da documentazione — `winner_market` prova la forma osservata
  (185/186) PRIMA e la forma documentata (171/172) come FALLBACK
  (`WINNER_MARKET_ID_LEGACY`). Struttura reale: `bookmakerOdds → {book} →
  markets → {marketId} → outcomes → {outcomeId} → players["0"].price`.
  **Copertura Pinnacle eSports — CONFERMATA dal piano**: `/account` →
  `subscriptions[0].bookmakers` (dict, 348 chiavi) CONTIENE `pinnacle` +
  `betfair-ex`, `matchbook`, `sbobet`, `marathonbet` (gli sharp); quota del
  piano free **250 richieste/mese** (`request_count`/`request_limit`).
  L'assenza di prezzi Pinnacle sulle fixture eSports misurate e' quindi
  TIMING DI PUBBLICAZIONE (lo sharp pubblica tardivo, tipicamente vicino al
  match), non copertura del piano: le fixture senza prezzi sharp danno
  `None` fail-closed (nessun verdetto invece di un verdetto su un book
  ricreativo) e l'oracolo parte da solo quando Pinnacle pubblica.
  ⚠️ **429 = RATE LIMIT A RAFFICA, non quota esaurita**: il piano free limita
  le chiamate AL MINUTO (misurato: 429 dopo 2 chiamate consecutive rapide
  con quota ancora a 222 residui); un eventuale scanner deve fare PACING
  (il pattern `football_hist._throttle` e' il precedente). Quota consumata
  dalle diagnostiche del 29/09: ~28/250.
- **`esports_oracle.py` (nuovo, 67 test verdi TUTTI offline)**: tabella
  `ESPORTS_TITLES` + `SX_LABEL_TITLES` DETERMINISTICA (sconosciuto → None,
  mai fuzzy: un titolo sbagliato confronterebbe il prezzo con l'oracolo di
  un altro gioco); `winner_market` fail-closed su entrambi i lati (scarta
  `active=false` e `price<=1`) con doppia forma 185/186 + fallback 171/172;
  **i NOMI arrivano dalla riga fixture** (`team_names=`): il payload /odds
  reale ha solo `participant1Id/2Id` — `oracle_for_fixture` li passa con
  GUARDIA DI ORIENTAMENTO PER ID (payload invertito → nomi scambiati; id non
  coincidenti → fail-closed, mai un verdetto orientato a caso);
  `true_probabilities` DELEGA a
  `market_calib.market_implied` (zero formule copiate); orientamento nomi via
  `team_names.same_team` (inversione gestita, non agganciabile → None);
  `ev_gate`/`value_candidates`/`candidate_for` **IMPORTATI da
  pinnacle_oracle** (tripwire: `"p * (price - 1.0)" not in CODE`); `api_key`
  da env `ODDSPAPI_KEY` (iniettabile vince); `requests` import PIGRO
  (tripwire subprocess: importare il modulo non carica requests/tracker/
  decision); `_call` restituisce sempre `{ok,status,payload,error,code}` (mai
  eccezioni al chiamante); CLI con guard `__main__` (`--titles` senza rete,
  `--fixtures`/`--odds`/`--account` fail-closed exit 1 senza chiave).
  Tripwire: niente poisson/numpy/scritture/ordini nel CODE (ast-based).
- **Tripwire IaC**: classe `TestIaC` in `test_esports_oracle.py` —
  `ODDSPAPI_KEY`, `ODDSPAPI_BASE`, `ODDSPAPI_BOOK`, `ESPORTS_DEVIG_METHOD`
  devono stare in `preserve()` in `.railway/railway.ts` (lezione del 28/09:
  `config apply` distrugge cio' che non e' dichiarato).
- **Chiave**: `ODDSPAPI_KEY` impostata dal PROPRIETARIO su Railway ma
  **SOLO sul servizio `surebet`** (impostazione a livello ambiente presa dal
  cron): il `config plan` ha visto `1 to destroy` (variabile presente ma non
  dichiarata sul cron) e sul container `betting_bot` la chiave era ASSENTE.
  Fix: `preserve()` dichiarata ANCHE sul servizio surebet (piano pulito) e
  chiave copiata su `betting_bot` con PIPE DIRETTO fra i servizi
  (`railway variables --service surebet --kv | railway variable set --stdin`:
  valore mai in chat). Impronta sul container: **len 36, sha12
  `a6b0e8e539d2`**. Vault locale NON la contiene (i CLI locali restano
  fail-closed exit 1: comportamento atteso, la prova dal vivo si fa sul
  container). Redeploy automatico dopo il set: SUCCESS.

**6) VERIFICA IN PRODUZIONE (29/09, dopo deploy `bd780a2e` SUCCESS).**
- `/api/health` 200 (quota the-odds-api 335); import `esports_oracle` +
  `sx_realtime` OK sul container; `configured()` **True** dopo la copia della
  chiave; `python3 esports_oracle.py --account` risponde (endpoint
  non-metered vivo, nessun errore auth).
- Prova metered: `--fixtures lol` → fixture reali (CBLOL, LCS; a fine
  giornata il palinsesto era salito a 12 partite) con `fixtureId` in
  formato `id...`; estrazione end-to-end su dati reali verificata: market
  185/186, odds reali 1xbet 3.4/1.3, de-vig OK (p 0.2544/0.7456, overround
  6.3%), `value_candidates` = nessuno (corretto: quote sotto true_odd);
  Pinnacle senza prezzi sulle fixture odierne → `None` fail-closed, come da
  progetto (pubblica tardivo; nel piano c'e', vedi sopra).
- **CONFERMA ORACOLO**: l'oracolo si inizializza e legge in remoto
  (configured True + account 200 + fixtures + odds + de-vig + gate EV) con
  quota 28/250. Tre difetti documentali trovati e fixati sul campo (vedi
  sopra). Il limite operativo residuo e' SOLO il timing di pubblicazione
  Pinnacle sugli eSports: quando pubblica, l'oracolo e' pronto senza
  interventi.

### Gate di lega: nazionali in CORE + `league_dynamic.py` + chiave OddsPapi (30/09/2026)

**1) IL COLLO DI BOTTIGLIA ERA IL GATE DI LEGA, E LA SUA "FIX" ERA INERTE.**
Nelle 24h del 24/09 AFCON + UEFA Nations League erano l'88% delle righe
scartate (111 su 126) e l'unico motivo di stop del flusso: le nazionali giocano
quando i campionati di club sono in pausa, quindi il gate le vedeva mentre il
resto del calendario era fermo.
⚠️ **Il diff che sembrava risolverlo NON cambiava nulla.** Mappava
`"uefa nations league" -> "Nations League"` e `"africa cup of nations" ->
"AFCON"`, cioe' i nomi CANONICI verso nomi **assenti** da
`STRATEGY_LEAGUES`/`PROBATION_LEAGUES`: `canonical_league()` riscriveva il nome
corretto in un nome BLOCCATO, quindi `tier` restava `blocked` **prima e dopo**
(zero righe di flusso cambiate). **Regola**: un alias deve portare le VARIANTI
verso il nome canonico, **mai il contrario** — la direzione e' cio' che lo rende
utile o dannoso. Tripwire: `test_value_filter.test_alias_nazionali_puntano_al_
nome_canonico` (asserisce anche che NESSUN alias punti a una lega vietata).

**2) COSA E' STATO APPLICATO.**
- `UEFA Nations League` e `Africa Cup of Nations` in `STRATEGY_LEAGUES`
  (**CORE**), coi parametri PIU' PRUDENTI del core (`kelly_mult 0.8`,
  `max_stake 1.5%`, `min_edge 2.5pp` — gli stessi di Eredivisie).
  ⚠️ **ONESTA' SUL CRITERIO**: queste due **non hanno un ROI misurato
  positivo** (il backtest 2022-2026 copre i club). Il criterio storico del core
  NON e' soddisfatto: e' una direttiva del proprietario, non una misura, e il
  codice lo dichiara.
- Nomi = chiavi di `odds_api.SPORTS_MAP` (sono i nomi che il ledger contiene
  davvero: diagnosi 24/09 «lega 'UEFA Nations League' esclusa») e su cui girano
  rotazione quote + settlement.
- `odds_api.SPORTS_INTERVAL_DAYS`: entrambe **30gg -> 2gg**. Una lega ammessa a
  30gg e' **dormiente di fatto** e non produce candidati qualunque soglia
  (difetto misurato il 24/09). Costo mensile teorico rotazione **398.6/460**
  ✓ (`test_budget_mensile_piano_free`).
- `market_calib.LEAGUE_EFFICIENCY`: +0.55 (Nations League) / +0.50 (AFCON) per
  il blend dinamico.
- `ODDS_DAILY_BUDGET` su Railway **16 -> 24** (env, zero redeploy). Il 25/09 la
  misura sul giro reale aveva mostrato MLS 16ª lega e Liga MX 18ª nell'ordine
  effettivo: con un tetto piu' basso entrambe venivano rinviate al giorno dopo e
  le partite PERSE.
- Tripwire aggiornati DI PROPOSITO: `test_odds_api.test_rotazione_crediti`
  (`soccer_uefa_nations_league` era asserito `== 30`, ora `== 2`) e
  `test_stagger_non_anticipa_le_leghe_30gg` (usava Nations League come esempio
  di lega dormiente: ora usa `soccer_conmebol_copa_america`, che non e' ammessa).

**3) `league_dynamic.py` (NUOVO) — il gate MISURATO dal ledger.** Il gate era
una tabella statica mentre il ledger cresce; dal 22/09 `predictions.league`
esiste, quindi la strategia per campionato e' finalmente misurabile. Il modulo
legge il ledger in **sola lettura**, con i filtri CONDIVISI di era e fascia quota
(`tracker.filter_predictions`), e calcola per lega n/ROI/hit-rate/quota media
sulle SOLE righe giocabili (`PLAYABLE_TIERS`), con il conteggio **DICHIARATO**
delle righe escluse (era/fascia + non giocabili + senza lega): le due letture
sbagliate del 22/09 e del 25/09 erano nate da query manuali senza filtro d'era.
- **AUTORITA' (default prudente, stesso schema di `adaptive_weighting`)**: env
  `LEAGUE_DYNAMIC_ENABLED` **OFF** = sola telemetria; quando accesa puo' **solo
  RESTRINGERE**. **Non promuove MAI** una lega non ammessa: aprire una lega a
  denaro reale resta una decisione umana, perche' `significance.py` misura che
  con 30 chiusure a quota media 1.65 l'edge minimo distinguibile e' **~41%** —
  un campione di gate a taglia minima non e' una prova di profitto.
- Fail-open sulla lettura (telemetria rotta = nessuna restrizione) e fail-closed
  sul campione (sotto 30 chiusure non si conclude nulla).
- CLI: `venv/bin/python league_dynamic.py [--json] [--since D] [--odds-min X]
  [--odds-max Y] [--all]`.
- Env in `preserve()` (`.railway/railway.ts`): `LEAGUE_DYNAMIC_ENABLED`,
  `_SINCE`, `_MIN_SAMPLES`, `_DEMOTE_ROI`, `_PROMOTE_ROI`, `_TTL`.

**4) `ODDSPAPI_KEY` ERA TRONCATA SU PRODUZIONE (chiave rifiutata).**
Verifica richiesta dal proprietario: su `betting_bot` la chiave era **len 27 /
sha12 `fa614ddf6a12`** e OddsPapi rispondeva **HTTP 500** a ogni tentativo
(non transitorio). La chiave INTEGRA (**len 36 / sha12 `a6b0e8e539d2`**, il
formato UUID e l'impronta gia' documentata il 29/09) era rimasta su `surebet`.
Fix: **pipe diretto** `surebet -> betting_bot` (il valore non e' mai transitato
in chat), redeploy `a01a04e6` SUCCESS.
**Esito**: `--account` risponde **200** → `plan free`, `request_limit 250`,
`request_count 29`, `error: null`, **`pinnacle` presente nel piano** e
`sport_ids` con 16/17/18 (eSports). La copia di una chiave fra servizi con pipe
`railway variables --kv | ... --stdin` resta la procedura corretta (regola 7).

**5) BUG DI DISPLAY FIXATO (`esports_oracle.account_quota`).** La CLI leggeva
`payload["request_limit"]`/`["request_count"]` al livello SUPERIORE e stampava
**`None/None` con una chiave VALIDA** — cioè faceva sembrare rotta una chiave
che funzionava, e durante la verifica di integrita' si e' dovuto guardare il
JSON grezzo per distinguere "chiave rifiutata" da "campo letto nel posto
sbagliato". I campi vivono nella **sottoscrizione** (`subscriptions[]`, quella
di `current_subscription_id` o `is_active`). Nuovo `account_quota()` con ordine
sottoscrizione-corrente -> prima attiva -> prima -> livello superiore
(retrocompatibile); il display dichiara anche residuo/piano/validita' e, se la
quota manca, dice **"non dichiarata nella risposta (chiave accettata)"** invece
di stampare `None`. Output reale: `29/250 richieste usate (residuo 221 · piano
free)`. Test: `TestAccountQuota` (7) in `test_esports_oracle.py`.

**6) TRAPPOLA DELLA DATA FISSA (di nuovo) — 4 test rossi pre-esistenti.**
`test_live_intel._seed_match_and_signal` seminava il kickoff
`"2026-09-29T20:45:00Z"`, ma `decision.adapters.iter_signals` usa una finestra
**MOBILE** (`now..now+24h`): il 30/09 quella data era nel passato → `signals=[]`
→ 4 test di `TestDataAgentIntel` rossi **senza che nulla fosse rotto**.
Verificato con `git stash` che le failure erano pre-esistenti. Ora il kickoff e'
RELATIVO a `now`. E' la stessa lezione del 15/09 e del 17/09: **un test che
scade col calendario arriva sempre nel momento peggiore**.

**7) VERIFICA IN PRODUZIONE (30/09/2026).** Deploy `f0cabfac` (+ `a01a04e6`)
SUCCESS; `/api/health` **200**, `overdue_orphans 0`, crediti **325** (reset
01/10, ritmo 17.6/giorno), nessun traceback; cicli `auto_bet` ogni 60s con
`equity 33.55 · esposizione 0.00/13.42 · stake fisso 1.50`.
**Gate di lega SBL OCCATO, verificato sui valori esatti del log** (AFCON,
esito `2` @1.30, EV -30.4%): `league_allowed=True`, `tier=core`, e il motivo
dello scarto e' **`"EV troppo basso (-30.4% < 2%)"`** — NON la lega.
⚠️ **Il gate sbloccato non produce puntate**: quelle partite vengono
giustamente scartate dall'EV. Il gate era il blocco *strutturale*; ora si applica
il filtro che deve applicarsi. Le 209 previsioni aperte (95 AFCON + 30 Nations
League) sono righe `rejected` **storiche**, non candidati.
⚠️ **La riga di log `sx_signals.py:667` NON stampa `reason`**, quindi dal log da
solo NON si distingue "scartato per lega" da "scartato per EV": per questo la
verifica e' stata fatta con `is_sane(..., league=...)` sui valori del log. Non
dedurre il motivo dello scarto dallo `status` nel log.

**8) NOTA OPERATIVA STRUMENTI (30/09/2026).** In questa CLI
`railway ssh --service <svc> -- <cmd>` **non raggiunge il container**: atterra
sull'**agente Railway** (`railway.new`) e restituisce il banner dell'account,
**ignorando il comando** (verificato con 3 varianti di sintassi; nessun blocco
stale in `~/.ssh/config`). Per eseguire codice con le ENV del servizio usare
**`railway run --service <svc> -- <cmd>`** (gira in locale con l'ambiente
deployato): e' cosi' che sono stati verificati impronta della chiave, `--account`
e il gate. ⚠️ `railway logs` **streamma**: usare `--lines N` (storico, non
bloccante). ⚠️ `verify_guardrails.py` dura >5 min ed e' stato saltato (i suoi
scenari A-H restano da rilanciare fuori da una shell con timeout breve).

**9) FIX DEL DISPLAY QUOTA + LEZIONE SUI COMANDI CHE TRONCANO IL TURNO
(30/09/2026).** Il fix del display (`account_quota`: la quota vive in
`subscriptions[]`, non in cima al payload) e i 7 test sono stati deployati con
`e6868d9` -> deployment **`4f3101ef` SUCCESS** (23:35 UTC); health 200 dopo il
riavvio, ensemble riaddestrato al boot (n=54, brier 0.0392), backup integrity
ok, cicli `auto_bet` puliti.

⚠️ **Il terminale si e' chiuso 3 volte durante la sessione — causa trovata e
regola permanente.** Non era un crash dell'agente: (1) una chiamata a
`verify_guardrails.py` (>5 min) ha superato il timeout della shell a meta'
turno; (2) tre attese `sleep 60/90/120/200` hanno superato lo stesso timeout;
(3) il check di stato era **sbagliato**: `pgrep -f verify_guardrails.py`
intercettava la PROPRIA riga di comando (che contiene il pattern) e riportava
`ELAPSED 00:00`, quindi dichiarava "IN CORSO" su un processo gia' morto.
Regole: **niente comandi bloccanti e niente `sleep` lunghi** (usare
`--lines N` sui log e `--json` sugli stati, mai lo streaming); mai `pgrep -f` /
`pkill -f` con un pattern che compare nella riga di comando invocante.

### Oracolo eSports cablato + corsia LIVE (30/09/2026)

**Direttiva**: integrare `esports_oracle` in `auto_bet.py` e attivare la corsia
LIVE. Ricognizione fatta PRIMA di scrivere codice (tutto verificato sui valori
reali del servizio, non ipotesi): `AUTO_BET_MODE=live`,
`EXECUTION_PROVIDER=sxbet`, `ODDS_DAILY_BUDGET=24`, `ENABLE_LIVE_OU=1`,
`STAKE_CAP_HARD=0` erano **gia' presenti** su `betting_bot` → il passo "abilita
il live" e' stato un no-op; `EXECUTION_DRY_RUN` assente (dry-run spento) e
`ESPORTS_LIVE` assente (default di codice ON).

**Il problema non era "filtrare", era "non c'e' nessuno che guardi".** La
discovery di `sx_signals` (`sportIds=5`, `type=1`) e quella di `multi_market`
(`sportIds=5`) sono **solo calcio**, e `execution_engine.resolve_market_for`
usa `event_type_ids=("5",)`: nessun mercato eSports entrava nel sistema, quindi
non c'era nulla da filtrare. Serviva una **corsia nuova**, non un flag.

**`esports_lane.py` (nuovo)**: catena in 4 passi, **gratuita tranne l'oracolo**.
1. DISCOVERY (SX `sportId=9`, `type 52` = moneyline 2 vie senza pareggio,
   lettura pubblica, zero chiavi/crediti): su SX un mercato binario e' "X vs
   Not X" con `outcomeOne` = il lato comprabile con `selection 1`, e per una
   partita 2 vie esistono DUE mercati (uno per squadra), raggruppati per evento
   con chiave `(_norm(t1), _norm(t2), minuto kickoff)`.
2. ORACOLO OddsPapi (l'unica voce a pagamento): `esports_oracle.match_fixture`
   + `oracle_for_fixture` (probabilita' fair de-vigate di Pinnacle).
   **the-odds-api NON ha eSports**: senza oracolo esterno il gate top-down
   risponderebbe `no_oracle` su ogni riga e la classe resterebbe non giocabile
   per costruzione.
3. EV GATE: `esports_oracle.candidate_for` → `pinnacle_oracle.ev_gate`, soglia
   `value_filter.EV_MIN` **importata** (una sola definizione di soglia).
4. PICK nella forma attesa da `run_today_bets`, con `mercato="ML"`, `esito_key`
   1|2, `team` = lato da comprare, `match_id=sx-esports-<event_id>` e
   `market_id`/`selection_id` (servono al settlement).

**Perche' NON si applica il gate di lega (decisione dichiarata, non dimenticanza)**:
`value_filter.league_allowed` misura **campionati di calcio** con un ROI
storico; gli eSports sono un'altra classe di rischio senza misura e le etichette
SX (`League of Legends`, `CBLOL`...) non sono nel set ammesso → applicarlo
significherebbe che la corsia non puo' giocare NULLA per definizione. La
prudenza sta altrove: **fascia quota di produzione** (1.30-1.80 da
`value_filter`, override `ESPORTS_ODDS_MIN/MAX`), profondita' minima SX,
oracolo sharp de-vigato e **stake fisso 1.50** del recinto.

**Settlement eSports = SX-native, a costo zero.** `sx_signals._results_from_sx`
legge `markets/find` sui `market_id` salvati sulle bet
(`WHERE mode='live' AND esito_finale IS NULL AND match_id LIKE 'sx-%' AND
market_id != ''`), **senza riga in `matches` e senza the-odds-api**. E' il
motivo del prefisso `sx-esports-` e del `market_id`/`selection_id` nel pick:
senza quelli la riga non sarebbe saldabile e il capitale resterebbe
immobilizzato fino alla scadenza automatica.

**`execution_engine.resolve_moneyline_market` (nuova)**: sport 9 / type 52,
verifica che il `team` sia un partecipante, esclude i mercati **con linea**,
aggancia `outcome_one_name` **per nome** e ritorna `selection_id=1`,
`market_type="ML"`. Il grouping evento di `resolve_match_market` e' stato
estratto in `_unique_event_markets` e **riusato** (due copie divergerebbero).

**`auto_bet`**: `_esports_picks()` (fail-safe → lista vuota) entra nel board **solo
con `mode == "live"`** — l'oracolo costa quota e il suo unico scopo e' decidere
il PREZZO di un ordine reale. Il gate top-down Pinnacle e' **escluso** per
`mercato == "ML"` (legge le cache del **calcio**: su un pick eSports
risponderebbe `no_oracle` e ucciderebbe la corsia). `_live_fill` ha un branch
`is_ml` che usa il resolver moneyline (guardia: pick ML senza `team` → salto).

**BUG REALI trovati scrivendo/verificando** (non ipotesi):
1. `sx_signals.ev_gate` espone la probabilita' fair come **`prob`** (non
   `p_true`) e **NON** ripete la soglia (`ev_min` non esiste nella riga) → il
   codice sollevava `KeyError('ev_min')` inghiottito come "corsia non
   disponibile". Fix: `p_true = float(verdict["prob"])`, soglia da
   `eo.min_ev()`.
2. La prob. implicita del lato va mappata **per NOME squadra**, non per
   posizione: l'ordine con cui SX restituisce i due mercati non e' un
   contratto.
3. `ESPORTS_ORACLE_WINDOW_H`/`ESPORTS_MIN_INTERVAL_S` lette **all'import** non
   erano ne' tarabili ne' testabili → convertite in funzioni runtime
   `oracle_window_h()`/`min_interval_s()` (pattern `smart_hedging`) con
   `_num_env()` (valore impossibile → default + warning; negativo → 0).
4. **Il primo ciclo reale ha bruciato 6/6 richieste in un giro**: interrogava
   eventi a 4h+ dal kickoff (quando Pinnacle non ha ancora pubblicato) e due
   chiamate ravvicinate prendevano **HTTP 429** — e la 429 e' un **rate limit
   al MINUTO**, non una quota esaurita: la richiesta e' **persa** (quota
   pagata, zero dati). Fix: `_pace()` (distanziamento prima di ogni richiesta,
   default 2.5s) e `oracle_window_h()` (default 3h) che esclude dall'oracolo
   gli eventi lontani. Discovery e fascia quota restano a **24h** perche' sono
   gratuite.
5. In produzione il testo del log non distingueva "corsia dormiente" da
   "corsia rotta": aggiunto `events_in_oracle_window` a `summary()` (due
   conteggi DISTINTI: discovery 24h vs finestra oracolo) e la riga nel report.
   ⚠️ `summary()`/`--report` **NON sono gratis**: chiamano `picks()`, quindi
   consumano quota quando c'e' un evento in finestra.

**Log del primo ciclo della corsia (deploy `a407586d`)** — la corsia GIRA in
produzione e fallisce in modo **dichiarato**:
```
esports_lane: oracolo assente per Betboom Team vs OG (nessun Match Winner completo per 'pinnacle' ...)
esports_lane: fixtures 'valorant' non lette (HTTP 429)
esports_lane: budget OddsPapi esaurito per oggi (6/6) — nessuna richiesta
```
**Log dopo il fix (deploy `ed388951`, 00:31-00:37 UTC)** — tutti e 6 gli eventi
discovery sono a 9h+ dal kickoff → **zero richieste, zero costo** e silenzio
voluto (il log del salto finestra e' a DEBUG). Verificato coi valori reali:
`finestra oracolo 3.0h | ora UTC 00:31`, eventi 09:00 / 10:00 / 13:00 / 16:00 /
16:00 / 21:00 → **tutti "oltre"**. `oracle_window_h=3.0`, `min_interval_s=2.5`
letti dal container. **Zero HTTP 429 reali** nei log (le 2 occorrenze di "429"
erano i millisecondi del timestamp, `00:26:43,429`).

**Deploy**: `ca78363` (corsia, 9 file, +1519/-3) → `a407586d` SUCCESS;
`e80794d` (finestra + pacing + `events_in_oracle_window`) → `ed388951`
SUCCESS. **Env**: tutte le `ESPORTS_*` dichiarate `preserve()` in
`.railway/railway.ts`; `railway config plan` pulito (0 to destroy). I default
di codice valgono su Railway (nessuna env ESPORTS_* impostata a mano).

**⚠️ Pinnacle pubblica TARDIVO sugli eSports**: ogni fixture senza prezzi sharp
da' `None` **fail-closed** (nessun verdetto invece di un verdetto su un book
ricreativo). Sul piano free `pinnacle` E' incluso nelle sottoscrizioni
(`subscriptions[0].bookmakers`), quindi il limite operativo e' solo il timing:
la corsia parte da sola quando lo sharp quota. Aspettativa realistica:
**zero ordini eSports finche' non pubblica** — il fail-closed e' il progetto,
non un difetto.

**Regola permanente dagli incidenti**: `verify_guardrails.py` deve spegnere le
sorgenti di rete PRIMA di girare. Senza `LIVE_INTEL=0` il `DataAgent` faceva
**scraping reale di FBref** e la diagnostica moriva a meta' scenario C; ora
imposta `LIVE_INTEL=0` **e** `ESPORTS_LIVE=0` e completa in ~45s con
**A-H tutti bloccanti**.

#### Probe end-to-end reale dell'oracolo eSports (30/09/2026, 00:56 UTC)

Prova eseguita sul container con la chiave vera (`railway run --service
betting_bot`), **misurata in quota** con `/account` prima e dopo (endpoint non
metered). Esito:

```
eventi SX       : {valorant: 1, dota2: 5, lol: 1}        (discovery, gratis)
fixtures valorant: status=200 righe=6
   Vitality vs LOUD | 2026-09-30T09:00:00.000Z          <- e' l'evento SX
aggancio SX->OddsPapi: Team Vitality vs Loud <-> Vitality vs LOUD  ✅
ORACOLO         : ok=False, richieste=1,
                  "nessun Match Winner completo per 'pinnacle'"
QUOTA           : 42/250 -> 44/250 (probe: 2 richieste)
```

**Le tre conclusioni che contano.**
1. **La catena funziona end-to-end con la chiave vera**: discovery SX → fixture
   OddsPapi → aggancio per NOME → chiamata odds → verdetto. Non e' "corsia
   rotta": e' la corsia che **rifiuta di inventare** un verdetto.
2. **Pinnacle non ha pubblicato** il Match Winner di quell'evento alle 00:56 UTC
   per un match delle **09:00 UTC** (8h prima). Fail-closed come da progetto.
3. La fixture ESISTE su OddsPapi e i nomi agganciano (`Vitality` vs `LOUD` ↔
   `Team Vitality` vs `Loud`, via `team_names.same_team`).

**⚠️ GAP STRUTTURALE TROVATO (da decidere, non corretto)**: il TTL dei
`no_oracle` (`ODDS_MISS_TTL_MIN` 60 min) e' **piu' lungo della finestra
eseguibile**. La finestra ordini e' T-120..T-50 (`T60_WINDOW_MIN_MIN/MAX_MIN`),
quindi l'ultima interrogazione utile e' a ~T-50. Con TTL 60min: un miss a
T-120 viene ritentato a T-60 e poi **mai piu'** entro la finestra → se Pinnacle
pubblica a T-45 il pick **non viene mai visto**, anche col budget intatto.
Combinato col tetto di 6 richieste/giorno, la copertura realistica e' di **1-2
eventi al giorno**, e solo se lo sharp pubblica prima di T-50.

**Numeri per decidere** (tetto free 250/mese = ~8,3/giorno):
- 6/giorno = 180/mese (attuale, margine ampio);
- 8/giorno = 240/mese (al limite ma dentro il piano);
- copertura di UN evento a granularita' 15' su T-180..T-50 = ~9 richieste:
  insostenibile. Servono finestra piu' stretta (T-120..T-50) e TTL-miss corto.
Opzioni sul tavolo: alzare `ESPORTS_REQ_BUDGET_DAY` a 8, stringere
`ESPORTS_ORACLE_WINDOW_H` a ~1h, abbassare `ESPORTS_ODDS_MISS_TTL_MIN` a ~10-20
min. **Nessuna applicata**: e' una scelta di quota/strategia del proprietario.

**Quota OddsPapi al 30/09 00:56 UTC: 44/250 usate (residuo 206)** — di cui 6
consumate dal primo ciclo di produzione della corsia e 9 dalle diagnostiche di
questa verifica. Il consumo reale di un giorno "normale" a corsia dormiente e'
**0**.

**Lezione di metodo (errore mio, non del codice)**: `esports_oracle.fixtures()`
estrae GIA' le righe in `fixtures` del dict di ritorno; leggere
`(r.get("payload") or {}).get("fixtures")` da' **sempre 0 righe** e sembra che
il provider non abbia dati. Il probe ha cosi' prodotto **due falsi negativi**
("lol: 0", "dota2: 0") prima che la misura corretta dicesse 4 e 7. Un
diagnostico che legge la chiave sbagliata accusa il sistema esterno.
