# RECOVERY GATE 2 — QuotaVerace

**Data**: 2026-10-20 18:20 UTC · **Modalità**: sola lettura (container + ledger `mode=ro`)
+ test isolati offline · **Ordini piazzati**: 0 · **Config/DB/codice di produzione
modificati**: nessuno · **Crediti the-odds-api spesi**: **0** · **Deploy**: nessuno

---

## VERDETTO: **A — BLOCCATO**

Non c'è nessun guasto tecnico. Ci sono **tre blocchi di protezione attivi**, due dei
quali **non aggirabili senza capitale**, e un **quarto blocco di sostanza**: i tre
candidati non hanno edge dimostrabile al prezzo di adesso.

| # | blocco | stato | rimovibile? |
|---|---|---|---|
| 1 | **CB2** (equity 21.1004 ≤ 25.00) | armato dal 18/10 04:42:54 | solo con +3.8996 USDC (**+18.5%**) |
| 2 | **Kill-switch** (override su file = `off`) | dal 18/10 22:44:47 | sì (una env/file), ma da solo non basta: resta il CB2 |
| 3 | **Crediti** 31 con hard stop a 5, reset fra 11 giorni | non blocca l'ordine, blocca la verifica | sì, con top-up |
| 4 | **Edge dei candidati** | nessun EV positivo al prezzo equo | no (è una misura, non un gate) |

L'ordine dei blocchi è corretto per costruzione: il **kill-switch** è l'autorità
superiore e viene valutato **prima** di qualunque lettura del wallet.

---

## FASE 1 — PROTEZIONI

### 1.1 Il codice esatto che arma il CB2 e il kill-switch

| protezione | dove vive il codice | dove vive lo stato |
|---|---|---|
| **CB2 (muro patrimoniale)** | `auto_bet.t60_check_wallet_kill(equity)` arma; **blocca** in `auto_bet.run_today_bets` (gate in testa) e in `auto_bet.t60_dispatch_pending` | `data/execution/t60_kill.json` sul volume |
| **Kill-switch** | `auto_bet.kill_switch_status()` → `_kill_switch_override()` | `data/execution/auto_bet_mode.json` (override **prevale** su `AUTO_BET_MODE`) |
| **Stop giornaliero** | `auto_bet.check_daily_stop(bankroll, basis, basis_key)` | `data/execution/daily_stop.json` |
| **Stop settimanale** | `auto_bet.check_weekly_stop(...)` (rolling 168h) | `data/execution/weekly_stop.json` + `bankroll_history.json` |
| **Tetto per ordine** | `auto_bet.order_ceiling(equity)` ← `decision.stake_engine.aggressive_cap_usdc` | env `KELLY_MAX_STAKE_PCT` (0.12) |
| **Tetto per evento** | `auto_bet._event_guard(...)` → `tracker.claim_event_stake` (`BEGIN IMMEDIATE`, TTL 300s) | tabella `event_claims` |
| **Recinto** | `auto_bet.exposure_allows` / `open_exposure_status` | `bets` (esposizione dal ledger) |
| **Anti-duplicato** | `tracker.bet_exists_open_market` + claim atomico + dedup cross-corsia | `bets` + `event_claims` |
| **Rollback** | `auto_bet.clear_daily_stop` / `clear_weekly_stop` / `t60_clear_kill` (+ `/stopreset`, `/t60reset`) | — |

Il CB2 è **armato** (letto dal container, non dedotto):

```
{'triggered': True, 'threshold': 25.0, 'triggered_at': '2026-10-18T04:42:54.193310+00:00',
 'wallet_equity': 23.6901, 'reason': 'equity wallet 23.69 <= soglia 25.00 USDC',
 'file': '/app/data/execution/t60_kill.json'}
```

### 1.2 Perché il kill-switch risulta `off`

```
{'override': 'off', 'env_mode': 'live', 'requested': 'off', 'effective': 'off',
 'provider_ready': True, 'file': '/app/data/execution/auto_bet_mode.json'}
```

Non è un guasto né un default: è un **override persistente** scritto sul volume il
**18/10/2026 22:44:47 UTC** con motivo *"AUDIT CAPITALE 2026-10-19 — blocco richiesto
dal proprietario"*. L'env resta `live` (quindi se il file sparisse le puntate
tornerebbero armate): il file è l'unica cosa che tiene il sistema fermo, ed è
esattamente il meccanismo previsto dal kill-switch dell'08/09.

Nei log del giro (18:00–18:03 UTC) la conferma indipendente:

```
auto_bet: kill-switch OFF attivo - nessuna puntata        (ogni 60s, 0 ERROR)
```

### 1.3 Il CB2 impedisce *realmente* l'invio degli ordini?

**Sì, e su entrambi i percorsi che possono mandare ordini reali.** Verificato con test
isolati (wallet finto, provider finto, ledger temporaneo — **nessuna rete**):

| percorso | esito | test |
|---|---|---|
| `run_today_bets` (giro 60s) | 0 chiamate a `_live_fill`, 0 righe su `bets` | `test_giro_normale_nessun_ordine_con_cb2_armato` |
| `t60_dispatch_pending` (finestra T-60) | esce **prima** del gate di mercato e di `_live_fill` | `test_dispatch_t60_bloccato_dal_cb2` |
| controprova (equity 400) | 1 chiamata a `_live_fill` → il blocco viene dal CB2, non da altro | `test_senza_cb2_lo_stesso_giro_prova_l_ordine` |
| flag corrotto | trattato come **armato** (fail-closed) | `test_flag_corrotto_e_fail_closed` |
| soglia inclusiva | 24.90 e **25.00** armano; 25.01 no | `test_equity_sotto_soglia_arma_il_flag`, `test_equity_esattamente_alla_soglia_arma`, `test_equity_sopra_soglia_non_arma` |

**Due gap trovati (dichiarati, NON corretti — correggerli sarebbe code-change in
produzione, fuori dal perimetro di questo gate):**

* **G1 — `smart_hedging` non vede il CB2.** `smart_hedging._blocked_reason()` legge
  kill-switch, stop giornaliero e settimanale, ma **non** `t60_kill_status`: con il
  CB2 armato restituisce `None`, cioè la corsia hedge si considera autorizzata.
  Ogni sua gamba passa però da `auto_bet._live_fill`, quindi il danno è limitato —
  ma il *gate* non è dove dovrebbe essere. Oggi è irraggiungibile: il kill-switch è
  `off` e la corsia hedge esce prima.
* **G2 — percorso legacy del wallet.** Se la lettura del wallet fallisce nel percorso
  *legacy* (importo fisso), `run_today_bets` ripiega sulla **cassa simulata** e tenta
  l'ordine, invece di fermarsi. Il percorso di produzione oggi usato
  (`refresh_live_stakes`) è **fail-closed** (`wallet_non_leggibile` → 0 candidati), e
  l'importo fisso è spento (`ORDER_FIXED_STAKE_USDC=0`) → il ramo non è raggiungibile.

### 1.4 Stop giornaliero, settimanale, cap e posizioni massime

Stato runtime (tutti **non** armati — ma il CB2 rende la domanda accademica):

```
daily   {'stopped': False, 'day': '2026-10-18', 'start_bankroll': 26.9201,
         'until': '2026-10-19T04:42:39Z', 'basis_key': 'live_equity', 'loss_pct': 5.0}
weekly  {'stopped': False, 'until': None, 'window_h': 168, 'loss_pct': 12.0}
soft    {'configured': False, 'floor': 0.0, 'hard': 25.0, 'blocked': False}
```

| controllo | valore | verificabile | testato |
|---|---|---|---|
| stop giornaliero | **-5%** dell'equity di inizio giornata (= **1.0552 USDC**) | sì | **sì** (`test_perdita_giornaliera_oltre_5pct_blocca`, `..._sotto_soglia_non_blocca`) |
| stop settimanale | **-12%** dal picco rolling 168h (= **2.532 USDC**) | sì | **sì** (`test_drawdown_settimanale_oltre_12pct_blocca`, `..._sotto_soglia_non_blocca`) |
| tetto per ordine | **12%** equity = **2.532048** | sì | **sì** (`order_ceiling == cap_order_stake`) |
| tetto per evento | **12%** equity (claim atomico) | sì | **sì** (secondo claim respinto) |
| recinto aperto | **70%** = **14.7713** → **5** posizioni al tetto, **14** al minimo | sì | **sì** |
| correlazione | **30%** blocco = **6.3302** | sì | sì |
| cap giornaliero | **60%** = **12.6605** | sì | sì |
| **posizioni massime** | **5** (al tetto da 2.532) / **14** (al minimo da 1.00) | sì | **sì** |
| rollback | `/stopreset` (giornaliero), `clear_weekly_stop` (settimanale), `/t60reset` (CB2) | sì | **sì** (idempotenti) |

⚠️ **`/t60reset` NON basta con questa equity.** Il comando disinnesca il flag e poi
**rilegge** il wallet: a 21.1004 il CB2 si **riarma subito**. È il comportamento
corretto (protegge dal riarmo dopo un top-up dimenticato) — testato in
`test_riarmo_immediato_dopo_il_reset_sotto_soglia` e
`test_il_giro_riarma_da_solo_il_cb2_con_equity_bassa`.

### 1.5 Simulazioni in test isolato (richieste dalla direttiva)

| scenario | risultato | test |
|---|---|---|
| **equity sotto soglia** (24.90 / 25.00) | CB2 armato, 0 ordini | `test_equity_sotto_soglia_arma_il_flag`, `test_equity_esattamente_alla_soglia_arma` |
| **perdita giornaliera** (-5.0%) | blocco 24h, 0 ordini anche se l'equity risale | `test_perdita_giornaliera_oltre_5pct_blocca` |
| **errore API** (wallet) | percorso produzione: 0 candidati, 0 POST (**fail-closed**) | `test_wallet_non_leggibile_nessun_ordine`, `test_refresh_live_stakes_e_fail_closed` |
| **errore API** (percorso legacy) | tenta l'ordine con la cassa simulata → **FAIL** (G2) | `test_wallet_non_leggibile_percorso_legacy_GAP_DICHIARATO` |
| **ordine duplicato** | secondo claim respinto; bet aperta stesso mercato → `True`; DB rotto → `True` | `test_secondo_ordine_sullo_stesso_mercato_respinto`, `test_bet_aperta_blocca_lo_stesso_mercato`, `test_lettura_impossibile_e_fail_closed` |
| **claim scaduto** | innocuo: `claim_event_stake` esegue `DELETE FROM event_claims WHERE expires_at <= now` prima di ogni prenotazione | codice `tracker.py:2348` (nessun test dedicato) |

Tutti i test girano con `pytest` **offline**: wallet, provider e `_live_fill` sono
sostituiti da doppi in memoria; nessuna chiamata di rete, nessun credito, nessun ordine.

---

## FASE 2 — CANDIDATI

### 2.1 Cosa c'è davvero nel ledger (letto alle 18:17:45Z)

* `predictions` 1X2 aperte (ogni status): **81**
* con status giocabile (`value`/`strong_value`/`moderate`): **35**
* con **kickoff futuro**: **3** → gli altri 32 hanno il fischio d'inizio già passato
  (attendono il settlement: non sono candidati, sono arretrato)

| # | partita | lega | esito | quota segnale | nato | EV al segnale | T- (misura) |
|---|---|---|---|---|---|---|---|
| 1 | **FC Porto – PSV** | Champions League | `1` | 1.74 | 17/10 18:00 | +13.17% | **T-43** |
| 2 | **Napoli – Bodø/Glimt** | Champions League | `1` | 1.64 | 17/10 18:00 | +9.32% | **T-43** |
| 3 | **Tigres – León** | Liga MX | `1` | 1.89 | 19/10 04:00 | +18.22% | **T-522** |

### 2.2 Prezzi RILEVATI ora (order book SX, fetch 18:16:46Z)

**Le quote dei segnali hanno 3.01 e 1.59 giorni: non sono prezzi.** Il prezzo vero:

| # | quota segnale | **prezzo attuale** | movimento | size al best | depth | `inv_sum` | p equa (devig) | **EV @ prezzo** | **EV @ prezzo equo** |
|---|---|---|---|---|---|---|---|---|---|
| 1 Porto | 1.74 | **1.5414** | **-11.41%** | 2.837 USDC | 10.757 | 1.0100 | 0.6423 | **+0.25%** | **-0.99%** |
| 2 Napoli | 1.64 | **1.7167** | **+4.68%** | 816 USDC | 20.145 | 1.0050 | 0.5796 | **+14.44%** | **-0.50%** |
| 3 Tigres | 1.89 | — | — | — | — | — | — | **mercato assente** | — |

Lettura di questi numeri, senza girarci intorno:

* **Porto**: il prezzo si è **accorciato dell'11.4%** in tre giorni. Il modello
  (0.6504) ora concorda col mercato (0.6423) entro **0.8pp**: l'edge *c'era* tre
  giorni fa al 1.74, **è stato assorbito dal mercato**. +0.25% è sotto la soglia
  (1.0% per una lega core).
* **Napoli**: +14.44% è **divergenza modello-vs-mercato di 8.7pp**, non edge
  dimostrato. Se il modello avesse ragione, l'EV al prezzo equo sarebbe positivo;
  è **negativo (-0.50%)**. È la classe di errore già documentata (overconfidence sui
  bucket alti, 06/09; 1X2 giocabile pre-11/09 a -21.64%).
* **Tigres–León**: **600 mercati SX scansionati, 0 con Tigres o León**. Liga MX non è
  nel palinsesto corrente (le leghe presenti sono Brasileiro Serie B, CL, Copa
  Sudamericana, EL, La Liga, Primera A, Superettan, Championship). **Nessun ordine
  possibile**, a qualunque configurazione.

### 2.3 Eventi scaduti e mercati disponibili

| check | esito |
|---|---|
| kickoff futuro per tutti e tre | **PASS** (T-43, T-43, T-522) |
| mercato effettivamente quotato | **FAIL** (Tigres assente) |
| quote fresche (non 19 minuti né 3 giorni) | **PASS** — ririlevate 18:16:46Z e usate come prezzo |
| liquidità ≥ 20 USDC al floor | **PASS** (size al best 2.837 / 816 USDC, depth 10.7k / 20.1k) |

⚠️ **Nota sulla freschezza del dato**: `market_quotes` è aggiornata (ultimo
campione **18:16:09Z**) ma copre solo OU/AH/AH_OT/OU_OT/ML_OT — **non contiene
1X2** (verificato: `{'AH': 6587, 'AH_OT': 1767, 'ML_OT': 906, 'OU': 10122,
'OU_OT': 1821}`). Un audit che leggesse il
prezzo 1X2 da lì otterrebbe `NULL` su tutto, oppure una quota del segnale di tre
giorni. Il prezzo 1X2 va preso dall'order book SX, ed è quello che è stato fatto.

### 2.4 Probabilità indipendente — **UNKNOWN (non speso)**

Il percorso esiste ed è cablato: `line_oracle.fetch_h2h_for_pick(pick)` →
`pinnacle_oracle.sharp_odds_from_cache` → de-vig Shin. Copertura verificata:
`soccer_uefa_champs_league` contiene entrambe le partite in finestra.
**Costo stimato: 1 credito** (misurato, `markets=h2h`), non 3.

**Non è stato eseguito**, e i motivi sono tre, dichiarati:

1. i crediti sono **31** con hard stop a 5 e reset fra **11 giorni**: ogni spesa oggi
   si paga in copertura domani;
2. scriverebbe una **cache sul volume di produzione** (`toa_*`): fuori dal perimetro
   *sola-lettura* di questo gate;
3. **la decisione non cambierebbe**: i candidati sono bloccati a monte (CB2 +
   kill-switch), e il candidato 3 non ha nemmeno un mercato. Spendere per misurare
   qualcosa che non può diventare un ordine non è verifica, è consumo.

**Perché la stima è onesta**: 1 credito per lega, e il gate paga **solo** dentro la
finestra esecutiva. Non è "1 credito per candidato".

---

## FASE 3 — ECONOMIA (equity **21.1004 USDC**)

| grandezza | valore | come si legge |
|---|---|---|
| **rischio massimo per puntata** | **2.532 USDC** (12%) | `order_ceiling(21.1004) == cap_order_stake(9999, 21.1004) == 2.532048` |
| stake minimo eseguibile | **1.00 USDC** | `auto_bet.MIN_STAKE_EUR == execution_engine.EXECUTION_MIN_STAKE_EUR == 1.0` |
| rischio al minimo | **4.74%** dell'equity | sotto lo stop giornaliero (5%) |
| stop giornaliero | **1.0552** | -5% |
| stop settimanale | **2.532** | -12% |
| recinto aperto (70%) | **14.7713** | 5 posizioni al tetto, 14 al minimo |
| blocco correlazione (30%) | **6.3302** | |
| cap giornaliero (60%) | **12.6605** | |
| **gap dal muro CB2** | **3.8996** (+18.5%) | 25.00 - 21.1004 |

### 3.1 Il punto che rende il test live sconsigliato oggi — e non è il codice

**Una singola perdita al tetto (2.532) supera lo stop giornaliero (1.0552) e coincide
con quello settimanale (2.532).** Cioè: al tetto, **una** sconfitta fa scattare
entrambi i freni nello stesso istante. Per avere margine serve uno stake ≤ **1.06**:
a quel punto **due** perdite da 1.00 fanno già scattare il giornaliero.

Con 5 posizioni aperte al tetto il caso peggiore è **-12.66 USDC (-60%)** — un
drawdown che azzera il capitale operativo, non un "test limitato".

### 3.2 Il minimo è verificato, non assunto

`1.00 USDC` non è una convenzione: è il **floor sia del motore sia dell'exchange**
(`MIN_STAKE_EUR == EXECUTION_MIN_STAKE_EUR == 1.0`). Lo stake minimo è quindi
**compatibile** con le regole SX — ma **al 4.74% dell'equity è al limite** del
giornaliero. Con equity 21.1 il sistema è in una fascia dove *il minimo eseguibile* e
*il freno giornaliero* sono quasi lo stesso numero.

### 3.3 Soglie di arresto definite per il test (verificabili, testabili)

| soglia | valore | verificabile | testata |
|---|---|---|---|
| stop giornaliero | -5% equity di inizio giornata | sì (file + `daily_stop_status()`) | **sì** |
| stop settimanale | -12% dal picco 168h | sì (`weekly_drawdown`) | **sì** |
| CB2 | equity ≤ 25.00 | sì (`t60_kill_switch_status`) | **sì** |
| concentrazione | mai oltre 12% su un singolo evento | sì (`event_claims`) | **sì** |
| **rollback qualitativo** | **10 puntate con P/L < -3.00** oppure **residuo di riconciliazione > 0.50/giorno** | sì (`equity_reconciler`) | **no** (definita qui, non ancora esercitata) |

L'ultima è la soglia che mancava: i freni guardano l'equity *aggregata*, ma un test
live va chiuso anche quando l'equity regge e il **processo** non torna (settlement che
non chiude, residuo inspiegato). Definirla prima è l'unico modo per non deciderla nel
momento peggiore.

---

## FASE 4 — CONCLUSIONE

### **A — BLOCCATO**

Motivi, in ordine di forza:

1. **CB2 armato e non disinnescabile**: `/t60reset` a 21.1004 si riarma subito. Servono
   **+3.8996 USDC**. Nessun giro di configurazione lo evita, ed è giusto così.
2. **Kill-switch `off` per scelta tracciata** (18/10 22:44:47, "AUDIT CAPITALE"): non è
   un guasto da riparare, è uno stato da decidere.
3. **Nessun candidato con edge dimostrabile**: al prezzo equo del book l'EV è
   **-0.99%** (Porto) e **-0.50%** (Napoli); il terzo non ha mercato. Il +14.44% di
   Napoli è il modello che si allontana dal mercato, non il mercato che sbaglia.
4. **Crediti 31** con hard stop a 5 e reset fra 11 giorni, contro budget oracolo
   autorizzati da **36 crediti/giorno** sulla sola linea (~25x il residuo).

### Cosa serve per arrivare a **C — CANDIDATO A TEST LIVE LIMITATO**

In ordine, senza scorciatoie:

1. **equity ≥ 25.01** (raccomandato **≥ 30.00**: a 25.01 il CB2 è a un errore di
   distanza e il minimo eseguibile vale il 4%);
2. **`/t60reset` DOPO il deposito** (mai prima: si riarma), poi `/autobet live`;
3. **crediti ≥ 60** e budget oracolo ridotti (linea 12 → 2, gridiron → 0 per il torneo
   chiuso): un test live deve poter *verificare*, non solo eseguire;
4. **stake forzato a 1.00 USDC** (fisso) finché il capitale non sale: al tetto attuale
   una singola perdita vale l'intero freno settimanale;
5. **chiudere G1 e G2** (CB2 in `smart_hedging`, fail-closed sul wallet legacy): due
   modifiche piccole, da fare *prima* e non durante;
6. **un candidato con EV positivo sia col modello sia al prezzo equo**. Oggi non
   esiste, e non è un problema di configurazione: è il mercato che ha già assorbito
   i due segnali.

Nessuna di queste sei condizioni è stata forzata: la direttiva vieta di disarmare il
CB2 o di attivare il LIVE, e nessuna delle due è stata toccata.

---

## File modificati

| file | tipo | contenuto |
|---|---|---|
| `research_isolated/tests/test_recovery_gate2.py` | **nuovo** | 39 test offline sulle protezioni reali |
| `research_isolated/out/RECOVERY_GATE2.md` | **nuovo** | questo report |
| `research_isolated/out/RECOVERY_GATE2.json` | **nuovo** | versione machine-readable |
| `conftest.py` | **modificato (solo test)** | eccezioni dichiarate per esercitare le protezioni vere (`test_recovery_gate2`, `test_event_cap`, `test_order_watch`, `test_capital_enclosure`); le soglie non sono state toccate |

**Codice di produzione modificato: nessuno.** Nessun deploy. Nessun ordine.

## Esito dei test

```
research_isolated/tests/test_recovery_gate2.py ................  39 passed
test_capital_enclosure + test_order_watch + test_event_cap ...  103 passed
test_auto_bet + test_auto_bet_live + test_resting_orders .....  174 passed
```

**Totale: 316 test verdi**, tutti **offline** (ledger SQLite temporaneo, wallet e
provider finti, nessuna rete, **0 crediti**, **0 ordini**).

## Stato del sistema alla chiusura (letto, non dedotto)

```
equity        21.1004  (available 21.1004, exposure 0.0, fonte ledger)
esposizione   0 ordini    resting 0    claim aperti 0 (1 scaduto, innocuo)
CB2           ARMATO (soglia 25.00, dal 18/10 04:42:54)
kill-switch   off (override file, 18/10 22:44:47)
daily/weekly  non armati
crediti       31 (reset fra 11 giorni, hard stop 5)
health        200
```

**Nessun ordine e' stato piazzato in questa sessione**: `bets` = 80 righe, l'ultima e'
la **#80** del **18/10 04:01:41 UTC** (stake 2.07) — cioe' 41 minuti prima che il CB2
si armasse. E' anche la riga a cui corrisponde il claim scaduto trovato su
`event_claims` (`0xf8bcb06f…:1`, 2.07, scaduto 04:06:41): coerente, e innocuo.
