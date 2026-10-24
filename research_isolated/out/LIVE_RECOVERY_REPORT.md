# LIVE RECOVERY REPORT — QuotaVerace

**Audit del 20/10/2026, 17:20 UTC · sola lettura · nessun ordine, nessun deploy,
nessuna modifica di configurazione, database o codice di produzione.**

Fonti runtime: `railway ssh` sul container `betting_bot` (progetto
`creative-vibrancy`, region `ams`), SQLite aperto in `mode=ro`, API pubblica SX
Bet, `GET /api/health`. I numeri di saldo, crediti e ledger sono stati **letti
adesso**: nessun valore viene ripreso da report precedenti.

---

## VERDETTO: **A — BLOCCATO**

Il sistema non può tornare LIVE adesso, e **non per un difetto tecnico**:
l'infrastruttura è sana e le protezioni funzionano — funzionano così bene che
**arresterebbero il primo ordine**. Il capitale è sotto il muro patrimoniale.

| # | Blocco | Dato misurato |
|---|---|---|
| **1** | **CB2 armato e non disinnescabile** | flag `t60_kill.json` scritto il **18/10 04:42:54** con `wallet_equity 23.6901 ≤ 25.00`; equity attuale **21.1004**. `/t60reset` cancella il flag e **rilegge il wallet**: 21.10 ≤ 25 → **riarmo immediato** («CB2 RIARMATO IMMEDIATAMENTE») |
| **2** | **Kill-switch di modalità su file** | `auto_bet_mode.json` → `{"mode": "off"}`; `kill_switch_status()` = `{override: off, env_mode: live, effective: off, provider_ready: true}` |
| **3** | **Crediti quasi esauriti (hard stop 5)** | **31 residui** (hard stop a 5), consumo **70.7/giorno**, sostenibile **2.8/giorno**, esaurimento previsto **21/10**. L'oracolo indipendente (Pinnacle) diventa cieco entro le prossime **~26 chiamate da 1 credito** (o **~8** da 3) |

Sotto questi tre vincoli il bot gira, classifica e logga — e **non ordina**:
i log di ogni ciclo (60 s) recitano `modalita' 'off' (kill-switch o
fail-closed): nessuna puntata`. Zero ordini dal **18/10 18:14**.

**Nota di merito**: nessuno dei tre blocchi è un'anomalia. Il CB2 ha fatto
esattamente il suo mestiere (equity scesa sotto la soglia), il kill-switch è
una scelta operativa e i crediti sono un tetto di piano. Ma la domanda
dell'audit — *«può tornare LIVE adesso con rischio limitato?»* — ha risposta
**no**, e la risposta non dipende da una decisione tecnica: **serve capitale**
(equity > 25 USDC) e **crediti** prima di qualunque altra considerazione.

---

## FASE 1 — STATO RUNTIME (verificato, 17:20 UTC)

### Saldo e capitale

```
available   21.1004 USDC
exposure     0.0000 USDC
equity      21.1004 USDC      (snapshot non stale)
bet aperte   0 / ordini resting 0 / stake immobilizzato 0.00
```

Nessun capitale bloccato fuori dal ledger: il conto è interamente liquido.

### Protezioni e limiti effettivi

| Voce | Valore | Note |
|---|---|---|
| CB2 (patrimoniale) | **ARMATO**, soglia 25.00 | armato 18/10 04:42:54 a equity 23.69 |
| soglia morbida CB2 | non configurata (0.00) | — |
| stop giornaliero | **non armato** (`stopped: false`, base 26.9201 del 18/10) | |
| stop settimanale | **non armato** (`loss_pct 12%`, finestra 168h) | |
| settlement | **non in pausa** | |
| dry-run | **False** | |
| finestra esecutiva | **T-720 → T-2** | |
| tetto per ordine | **2.532 USDC** (12% di 21.10) | |
| ticket minimo motore | 1.00 USDC | floor exchange = 1.00 |
| recinto esposizione aperta | 70% → **14.77 USDC** | |
| cap correlazione | 30% → 6.33 USDC per blocco | |
| profondità richiesta | max(stake×1.6, 20) → **20.00 USDC** a stake 1.50 | |
| feed di mercato | **non bloccato** | `blocked: false` |

### Ultimi settlement e riconciliazione

Ultimo referto: **bet #80** (`1X2`, vinta, **+1.80 USDC**) il **18/10 18:14:04Z**.
Dopo quell'istante: **0 settlement, 0 ordini, 0 movimenti**.

Riconciliazione sulla finestra dei campioni di equity (18/10 01:17 → 22:33):

```
equity        26.9201 -> 21.1004        = -5.8197
ledger        6 settlement               = -5.7800
residuo                                   -0.0397   (0.7% del movimento)
verdetto      RICONCILIATO (arrotondamento)
```

**Residuo storico aperto**: il **-2.26 USDC** del 17/10 **non è riproducibile**
nella finestra attuale e **non è attribuibile**: SX Bet non espone lo storico
ordini (7 endpoint provati in passato: `/orders-v3` restituisce solo gli ordini
aperti, `/orders` 400 "OrderBook V2 no longer supported", `/user/orders` 400
"address must be valid"). Resta un limite dichiarato, non un'anomalia nuova.

### Errori nei log

Nessun `429`, `401`, `403`, `Traceback`. L'unico `ERROR` ripetuto è il messaggio
**intenzionale** del kill-switch. `GET /api/health` → **200**, con
`settlement.open.bets = 0` e `overdue_orphans` non popolato.

### Controlli superati / falliti

**Superati (9)**

1. Salute del servizio: health 200, tre cicli `auto_bet` puliti, nessun
   `429/401/403/traceback`; l'unico `ERROR` è il messaggio *intenzionale*
   `modalita' 'off'`.
2. Nessun capitale immobilizzato fuori dal ledger: 0 bet aperte, 0 ordini
   resting, escrow 0.00.
3. Riconciliazione della finestra corrente: residuo **−0.04 USDC** su −5.82 di
   movimento (0.7%), arrotondamento.
4. Aritmetica di settlement: **0 discrepanze** su 1.294 righe contro
   implementazione indipendente.
5. Commissioni: **0%** misurate, coerenti col P/L (`profit == (quota−1)·stake`).
6. Slippage: **80/80** fill a prezzo di segnale o migliore (max +4.6%).
7. Protezioni presenti: dedup `(market_id, selection_id)`, claim atomico
   `event_claims`, freschezza feed 20′, ri-risoluzione mercato, floor EV,
   guardia di profondità, finestra T-720..T-2.
8. Feed di mercato non bloccato (`blocked: false`).
9. Una sola fonte di verità per stake/tetto/recinto: `order_ceiling(21.10)` =
   `cap_order_stake(5.00, 21.10)` = **2.532**.

**Falliti (4)**

1. **CB2 armato** con equity 21.10 < 25.00 → il riavvio LIVE si auto-arresta
   (riarmo immediato dopo `/t60reset`).
2. **Crediti 31** (hard stop 5; sostenibili 2.8/giorno contro 70.7 misurati) →
   l'oracolo indipendente non copre un test significativo.
3. **Edge non dimostrato a livello di denaro**: 80 bet, −15.39 USDC, ROI
   −7.39%.
4. **EV dichiarato AH/OU non valido** sulle righe pre-16/10 → ogni analisi
   "EV vs ROI" su quei mercati è inutilizzabile.

---

## FASE 2 — SEGNALI REALMENTE ESEGUIBILI

### Cosa c'è davvero (dati reali)

- **35 righe 1X2 aperte** con status giocabile (`strong_value` 10, `value` 25);
  dopo i gate di lega, finestra e dedup la corsia ne seleziona **3**, tutte
  nella finestra esecutiva:

| partita | lega | kickoff UTC | esito | quota | prob mercato | prob modello | EV modello |
|---|---|---|---|---|---|---|---|
| Tigres – León | Liga MX | 21/10 03:00 | 1 | 1.89 | 0.5200 | 0.6255 | +18.22% |
| Porto – PSV | Champions | 20/10 19:00 | 1 | 1.74 | 0.5775 | 0.6504 | +13.17% |
| Napoli – Bodø/Glimt | Champions | 20/10 19:00 | 1 | 1.64 | 0.6068 | 0.6666 | +9.23% |

- **Quote di mercato fresche**: `market_quotes` 21.177 righe, ultima scrittura
  **17:00:59Z** (19 minuti). ⚠️ Il ledger copre **OU / AH / AH_OT / OU_OT /
  ML_OT**: **non contiene il 1X2**. Per il 1X2 prezzo e profondità esistono solo
  al momento dell'ordine (lettura del book pubblico, gratuita) — vedi "dati
  mancanti".

### Cosa è soltanto simulato o telemetrico

- **OU e AH: 0 ordini reali in tutta la storia del conto.** Le 295 righe chiuse
  (`AH strong` 79, `OU strong` 216) sono **telemetria**, non denaro.
- Il **torneo Gridiron è chiuso** (finestra 08/10 13:00Z → **20/10 13:00Z**):
  la corsia football USA non ha più un obiettivo e continua a consumare oracolo
  (96 dei 138 crediti delle ultime 48h sono NFL+NCAAF).

### Cosa manca (dichiarato, non dedotto)

1. **Probabilità indipendente dei 3 candidati**: non calcolata in questo audit
   (nessun credito speso). La calcolerebbe il gate top-down on-demand — 1
   credito per lega, con **31 crediti totali** residui.
2. **Prezzo/profondità eseguibili ora** per i 3 pick: non verificabili offline.
3. **Storico ordini** (accettati / cancellati): l'exchange non lo espone.

### Costi e slippage

- **Commissioni: 0%** — verificato per identità su **1.294 righe saldate**:
  il P/L di una vincita è esattamente `(quota − 1) × stake`. Il modello di
  costo **non** include commissioni perché su SX Bet non ce ne sono; se un
  giorno cambiassero, `value_filter.compute_ev` andrebbe corretto (il break-even
  passerebbe da `1/q` a `(1+f)/q`).
- **Slippage: 80/80 fill a prezzo di segnale o migliore** (massimo osservato
  **+4.6%**: bet #73 fill 1.9417 contro segnale 1.8561). Il floor EV
  (`riempi solo a prezzo ≥ quota del segnale`) funziona: nessuno slippage
  avverso registrato. Non è modellato a priori: è *applicato* dall'esecutore.

### Protezioni contro quote obsolete / mercato sospeso / duplicati

| Protezione | Presente | Meccanismo |
|---|---|---|
| quote obsolete | ✅ | gate `decision/feeds` con `DECISION_FEED_MAX_AGE_MIN=20` + **rilettura del book al momento dell'ordine** |
| mercato sospeso | ✅ (indiretta) | l'ordine ri-risolve il mercato su `/markets/active`: un mercato sospeso non è risolvibile → skip fail-closed |
| duplicati | ✅ | `bet_exists_open_market(market_id, selection_id)` + **claim atomico** `event_claims` (`BEGIN IMMEDIATE`) + dedup cross-corsia |
| prezzo fuori edge | ✅ | floor EV: fill solo a quota ≥ segnale |
| liquidità | ✅ | profondità minima al floor `max(stake×1.6, 20)` USDC |
| timing | ✅ | finestra T-720..T-2 + pavimento assoluto a 2 minuti |

---

## FASE 3 — CORRETTEZZA DELLA STRATEGIA

### 3.1 L'aritmetica di settlement è corretta (verificata, non assunta)

Ho ricostruito il verdetto di **1.294 righe chiuse** (OU 840: 490 linee .5,
350 linee intere con push; AH 454, di cui 10 quarter) con
un'implementazione **indipendente** e confrontato con il ledger:

```
discrepanze: 0
OU P/L registrato  30.256  = corretto  30.256
AH P/L registrato -16.981  = corretto -16.981
```

Push, split delle quarter AH e linee intere sono **esatti**. Nessuna
"scommessa saldata col risultato sbagliato" in questo insieme.

### 3.2 Difetto aperto: la quarter line dell'OU non è splittata

`tracker.ou_won` ricava la linea dal testo e la confronta col totale come
**soglia secca**. Su una linea `.25`/`.75` è sbagliato:

```
Over 2.75, 3 gol, quota 2.00
  corretto: metà 2.5 vinta (+0.50) + metà 3.0 push (0)   = +0.50
  prodotto: 3 > 2.75                                     = +1.00   ← il doppio
Under 2.75, 3 gol → corretto -0.50, prodotto -1.00
```

**Impatto storico: ZERO** — nel ledger non esiste **nessuna** riga OU in
quarter line (490 half, 350 intere, **0 quarter**). È un difetto **latente**:
diventerebbe denaro sbagliato il giorno in cui una quarter OU venisse giocata.
Il caso è ora fissato da un test `xfail(strict=True)`, così la correzione
futura romperà il test e obbligherà ad aggiornare questo report.

### 3.3 EV vs risultati su AH/OU: la discrepanza è **d'era**, non di formula

La formula attuale è corretta e push-aware:

```
EV = p_win × (quota − 1) − p_lose          (verificata su ogni candidato)
```

Ma sulle righe storiche **fino al 15/10** l'`ev` memorizzato **non** rispetta
questa formula: differenza media **+0.85** per AH e **+0.16** per OU, con
punte fino a **+11.64** rispetto a `prob×quota−1`. Il caso limite misurabile:

```
id 75062  "Away -2.5"  quota 18.60  prob 11.5%  ev memorizzato +1278%
            p_win reale = 0.037  →  EV corretto = −0.307
```

Un EV del +1278% con una probabilità dell'11% e una quota di 18.6 è
**matematicamente impossibile** (il payout massimo è 17.6). Dal **17/10** le
righe nuove hanno differenza **esattamente 0**: la formula è stata allineata.
Conseguenza diretta, da mettere agli atti: **tutta la statistica "EV dichiarato
vs ROI" su AH/OU costruita sulle righe pre-16/10 non è utilizzabile**. E i
mercati AH/OU non hanno **mai** prodotto un ordine reale: la loro "performance"
è telemetria di candidati, non P/L.

### 3.4 Quello che il denaro dice davvero

```
80 bet LIVE chiuse     staked 208.145     P/L -15.39     ROI -7.39%
  1X2    34 bet        staked  99.935     P/L  -9.94     ROI -9.95%
  TENNIS 46 bet        staked 108.210     P/L  -5.45     ROI -5.04%
  AH/OU   0 bet
```

Contro, il **CLV** (misura indipendente dai risultati) è **positivo**:

```
58 campioni      media +3.67%      mediana +3.28%      89.7% positivi
  pinnacle_t0    n=49  +4.44%
  sx             n= 9  -0.53%
status: beating (soglia minima 30 campioni)
```

Questa è l'unica evidenza *a favore* della strategia: compriamo sotto la
chiusura sharp in ~9 casi su 10. Ma **non basta**, e va detto con i numeri: con
58 campioni e questo profilo, il minimo edge distinguibile resta molto sopra il
+3.7% osservato; e il ROI realizzato su 208 USDC puntati è **-7.4%**. CLV
positivo + ROI negativo è compatibile con la varianza, **non è una prova di
profitto**. La soglia del progetto (30 chiusure per *concludere*, ~12.700 per
confermare un edge del +2%) non è stata raggiunta in nessuna delle due
direzioni.

**Conclusione Fase 3:** la formula è corretta, il settlement è corretto, i costi
sono modellati correttamente **di fatto** (0 commissioni, fill mai peggiore del
segnale). L'edge **non è dimostrato**: né dal ROI, né da un campione CLV
sufficiente. Non esiste oggi una base per classificare "C".

---

## FASE 4 — DECISIONE

### Cosa servirebbe per un test LIVE limitato (condizioni precise)

1. **Equity wallet ≥ 30.00 USDC** (margine ≥ 5.00 sul CB2 a 25.00 = ~3 puntate
   da 1.50). Con 21.10 il CB2 si riarma immediatamente: qualunque `/t60reset`
   è inutile finché il saldo non sale.
2. **Crediti the-odds-api ≥ 60** prima del primo ordine, e
   `ORACLE_BUDGET_DAY` **12 → 2** (percorso a linea: 0 ordini in tutta la sua
   storia) + budget oracolo **gridiron → 0** (torneo chiuso il 20/10 13:00Z).
3. **CB2 disinnescato solo DOPO il deposito** (`/t60reset`), poi `/autobet live`.
4. **Perimetro ristretto**: solo **1X2**, stake fisso **1.00 USDC**, **massimo
   1 bet aperta** fino a 20 chiusure; nessun OU/AH (zero ordini storici, EV
   storico invalido), nessun gridiron fuori torneo.
5. **Gate attivi**: lega core (probation a stake ridotto), finestra
   **T-720..T-2**, stop giornaliero 5%, settimanale 12%, CB2 25 — tutti
   **invariati**.
6. **Criterio di accettazione per ordine**: fill ≥ prezzo del segnale, `bet_id`
   presente, settlement entro 24 h, residuo di riconciliazione giornaliero
   < 0.10 USDC.
7. **Rollback**: 10 puntate con P/L cumulato < **-3.00 USDC**, oppure residuo
   inspiegato > **0.50 USDC** → `/autobet off` e nuovo audit.

### Limiti da non superare (per non falsare il test)

- **Non** disinnescare il CB2 senza deposito (si riarma e l'arresto resta).
- **Non** abbassare `T60_KILL_WALLET_USDC` per far passare un ordine.
- **Non** alzare `KELLY_MAX_STAKE_PCT` per aggirare il tetto di **2.53 USDC**.
- **Non** riaccendere l'oracolo a linea OU/AH senza `line_intersection` a
  supporto (misurato: griglia SX a passi di 0.5 vs main line Pinnacle).

### Mercati candidati (se e solo se le condizioni sopra sono soddisfatte)

Solo **1X2** sui 3 pick in finestra (`_today_value_picks()` → 3 su 35 righe
potenzialmente giocabili), e **solo se il gate top-down li approva**
(non verificato in questo audit: richiede 1 credito per lega). Nessun altro
mercato è candidabile oggi.

### Rischi minori (non bloccanti, da tenere presenti)

- Volume **224M/434M (53%)**; `book_flow_events.jsonl` **68 MB** in crescita.
- Il **tetto per ordine è derivato a ogni ciclo**: `order_ceiling()` legge
  `_LAST_BANKROLL`, che in un processo appena avviato vale 0 → il tetto è 0.0 e
  il percorso d'ordine è **fail-closed** (non un buco: nessuno ordina senza
  base). Da ricordare riavviando: la prima valutazione utile è al primo ciclo
  di `run_today_bets`, non prima.
- Residuo storico **-2.26 USDC** non attribuibile.
- 39 push su TENNIS da scadenza senza risultato: il ROI tennis include push a 0.
- 81 previsioni aperte in telemetria (16 senza riga partita, 18 in leghe non
  mappate): costo 0, ma il residuo non scende finché non scadono.

---

## Artefatti di questo audit

| File | Contenuto |
|---|---|
| `research_isolated/out/LIVE_RECOVERY_REPORT.md` | questo report |
| `research_isolated/out/LIVE_RECOVERY_RESULTS.json` | fotografia runtime, controlli, rischi, condizioni |
| `research_isolated/tests/test_ev_formula.py` | **37 passed, 1 xfailed** — formula EV, push, quarter line, settlement, commissioni, prezzo, regressione `analyze_fixture` |
| `research_isolated/lab/ou_settlement.py` | specifica eseguibile della quarter line OU (riferimento indipendente) |

I test girano **offline** (`venv/bin/python -m pytest
research_isolated/tests/test_ev_formula.py -q`): nessuna rete, nessun DB,
nessuna credenziale, nessun ordine.

---

**Nessuna azione sulla produzione è stata eseguita**: nessun ordine, nessun
disarmo del CB2, nessuna modifica di configurazione, database o codice, nessun
commit, nessun deploy.
