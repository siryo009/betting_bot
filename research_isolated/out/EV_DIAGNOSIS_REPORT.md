# Diagnosi dell'EV e ricerca di una strategia con aspettativa positiva

**Data**: 20 ottobre 2026 · **Snapshot**: copia di produzione del 20/10/2026 07:55 UTC
(`research_isolated/snapshot/quotaverace_prod.db`, sola lettura) · **Ambito**: nessun
ordine inviato, nessuna modifica a codice di produzione, CB2 e kill switch non toccati.

**Esito in una riga**: **non esiste oggi una strategia con aspettativa positiva
dimostrabile su questa infrastruttura**, e la ragione non è il modello di
probabilità ma il **prezzo pagato**: il CLV sul prezzo eseguibile è **−3,33%**
(IC95 [−4,37, −2,24], n=58), cioè compriamo stabilmente **sotto** la closing
line sharp. Il CLV di +1,70% / +3,34% riportato nei report precedenti **non è
riproducibile** da nessuna sorgente di chiusura (vedi §2).

---

## 1. Inventario dei dati di esecuzione

Tutto ciò che riguarda il denaro sta in 6 tabelle dello snapshot. Distinzione
**reale / simulato** verificata, non assunta.

| tabella | righe | cosa contiene | reale o simulato |
|---|---|---|---|
| `bets` | **80** | ordini effettivamente eseguiti (`price`, `market_id`, `selection_id`, `bet_id`, `status`) | **tutte reali**: `mode='live'` su 80/80, **0 righe SIM** |
| `predictions` | 2.200 | segnali emessi (incluse le righe `rejected`: telemetria) | reali (di cui **394 giocabili chiuse** con risultato) |
| `clv_history` | 448 | quota del segnale + chiusure | eterogenee: `closing_quota` 448 (**eco SX**), `pinnacle_quota` 355, `closing_odds` 86 (T-0 sharp), `betfair_odds` **0** |
| `market_quotes` | **21.112** | prezzi SX per fixture/mercato/linea, con `liquidity` e `updated_at` | reali (lettura pubblica SX), gateway `sxbet-multi` (743 fixture) e `sxbet-shadow` (460) |
| `price_snapshots` | 10.028 | storico prezzi per bookmaker | reali, **ma non-SX**: `pinnacle` 5.449 (+413 `Pinnacle`, case diverso), 1xBet 936, Betfair 876, Unibet 431, Coolbet 394 |
| `match_results` | 17.843 | punteggi e esito | reali (the-odds-api / API-Football / SX-native) |

### Cosa c'è davvero (e regge)

- **Prezzi SX con timestamp e liquidità**: sì, `market_quotes` copre 27/09 → 20/10
  con 100% delle righe con `liquidity > 0`.
- **Quota realmente pagata**: sì, `bets.price` (80 ordini, tutti con `bet_id`).
- **Risultati**: sì, e **verificati** (§3.1).

### Cosa MANCA (e perché conta)

1. **Lo storico del prezzo SX**: `market_quotes` è un **upsert** su
   `(fixture_id, market_type, line_key, selection)` — viene conservato **solo
   l'ultimo prezzo**. Non esiste una serie storica del prezzo SX, quindi non si
   può ricostruire "quale prezzo SX era disponibile quando il segnale è nato"
   né misurare lo **slippage** fra segnale ed esecuzione.
2. **Il book a due lati (bid/ask) di SX**: salviamo il solo prezzo *taker*. Senza
   bid/ask + depth per timestamp **non è misurabile il market making**, che è
   l'unica strategia coerente col fatto che il taker paga 3-4% sopra la closing
   (§4, §6).
3. **I movimenti di cassa**: depositi/prelievi non sono movimenti registrati,
   quindi il capitale non è ricostruibile dal ledger (limite già dichiarato nel
   report del 20/10).
4. **`betfair_odds`** (secondo benchmark sharp): colonna presente, **0 campioni**
   catturati.

**Fonti necessarie per colmare i buchi**: (a) un writer che, **prima**
dell'upsert, appenda `market_quotes` in un log JSONL append-only
(modello: `fill_rate`/`steam_move`); (b) un lettore del book completo
(`get_market_book` già esiste in `execution_engine`) che salvi bid/ask+depth con
timestamp; (c) movimenti di cassa come tabella. Nessuna delle tre richiede
credenziali nuove né crediti the-odds-api.

---

## 2. Le osservazioni con CLV positivo: perché erano un artefatto

Il punto di partenza erano **137 osservazioni con CLV medio +1,70%** e **40
osservazioni Pinnacle T-0 con CLV +3,34%**. Ricalcolate distinguendo **sorgente
di chiusura** e **prezzo realmente pagato** (`research_isolated/lab/clv_execution.py`,
decomposizione in `out/clv_execution.json`):

| misura | n | CLV medio | IC95 |
|---|---|---|---|
| quota del segnale vs `closing_quota` (SX) | 448 | **+0,13%** | — |
| quota del segnale vs `pinnacle_quota` | 355 | **−2,58%** | — |
| quota del segnale vs `closing_odds` (T-0) | 86 | **−3,00%** | — |
| **prezzo ESEGUIBILE vs T-0 sharp** | **58** | **−3,33%** | **[−4,37, −2,24]** |
| prezzo eseguibile vs `pinnacle_quota` | 49 | −4,04% | [−5,27, −2,82] |
| line shopping (acquisto − segnale) | 58 | −0,03% | — |
| P/L sui campioni | 58 | −15,80 USDC su 161,47 di stake | **ROI −9,78%** |
| **drawdown massimo della serie** (in ordine cronologico) | 58 | **35,68 USDC** | peggior esito singolo −5,61 |

**Stabilità** (finestre contigue, nessuna ottimizzazione): 1ª metà **−4,50%**
(n=29), 2ª metà **−2,16%** (n=29) → il **segno non cambia**, l'ampiezza sì.

**Per fasce di quota** (nessun sottogruppo positivo): 1,0-1,5 −1,31% (n=8,
IC fino a +0,09%), 1,5-1,8 −1,84% (n=20), 1,8-2,1 −2,80% (n=19),
2,1-3,0 −5,86% (n=7).
**Per corsia**: 1X2 −2,30% (n=29), tennis −4,36% (n=29).

### Le tre cause, misurate

1. **ECHI**: `closing_quota` è la quota SX riletta dal nostro stesso flusso. Il
   CLV che ne esce è **+0,13%** — non misura nulla, è il prezzo di partenza.
   Includerla in una media la sposta verso zero.
2. **DE-VIG DELLA CHIUSURA**: de-vigare la closing alza la quota di ~2pp;
   confrontarla con un prezzo d'acquisto che **contiene** il vig non è un
   confronto omogeneo. `clv_vig_free` serve a confrontare due closing fra loro,
   non una closing con un prezzo pagato.
3. **Non è line shopping**: la differenza acquisto−segnale è **−0,03%**, cioè
   l'ordine parte al prezzo del segnale. Il problema non è "il segnale è su un
   altro book": è che **il prezzo SX è esso stesso 3-4% sotto lo sharp**.

⚠️ **Trasparenza**: il **+1,70% non è ricostruibile** da nessuna singola colonna
di `clv_history` (le tre sorgenti danno +0,13%, −2,58%, −3,00%). Le cause
plausibili sono echi + de-vig + un campione con criteri propri; quel che è certo
e riproducibile è che **qualunque** sorgente presa, il CLV del prezzo eseguibile
è **negativo e significativo**. Nessuna delle 137 osservazioni è stata
"recuperata": la conclusione precedente non regge.

**Risposta al punto 2**: il risultato **non persiste** su dati nuovi né su
prezzi eseguibili, e **non è accompagnato** da rendimento positivo (ROI −9,78%
sugli stessi campioni). Il verdetto machine-readable è `lagging`.

---

## 3. Perché l'EV dichiarato è così distante dal realizzato

### 3.1 Settlement e contabilizzazione: prima di tutto, sono sani

Verifica indipendente (**non** una rilettura del codice): ricalcolo dell'esito
dai punteggi reali per **388 righe giocabili** con la logica 1X2/OU (push)/AH
(linea, split).

- **0 discrepanze reali** di settlement.
- **1 anomalia apparente** (`Home -0.75`, won con `profit 0.318` invece di
  `0.636`): è **corretta** — su una quarter line metà stake vince e metà è push,
  quindi P/L = (quota−1)/2. La contabilizzazione usa il P/L **per unità di
  stake intero**, coerente in tutto il ledger.
- Nessun match saldato con punteggio live, nessun push contato come perdita.

**Conclusione**: il divario EV non nasce da errori di referto o di contabilità.

### 3.2 Le probabilità: il mercato sa più del modello (in OGNI mercato)

373 righe decise (`out/ev_calibration.json`). `prob` nel ledger è **già il blend**
modello+mercato, cioè la probabilità che genera l'EV; `market_prob` è il mercato
devigato.

| mercato | n decise | hit realizzato | p usata (media) | Brier prob-usata | Brier mercato |
|---|---|---|---|---|---|
| 1X2 | 71 | 64,8% | 0,641 | 0,2255 | **0,2201** |
| AH | 75 | 64,0% | 0,689 | 0,2285 | **0,2251** |
| OU | 187 | 63,1% | 0,696 | 0,2274 | 0,2276 (pari) |
| TENNIS | 40 | 52,5% | 0,570 | 0,2167 | **0,2151** |
| **tutti** | **373** | **62,5%** | **0,671** | **0,2261** | **0,2243** |

La probabilità usata è **+3,7pp più alta** della probabilità di mercato
(0,671 vs 0,634) mentre l'hit realizzato è **62,5%**: overconfidence media
**−4,6pp**.

**Shrink ottimale** (fittato solo sul train 186, valutato su validation 93):
`w = 0,30` su `w·p_mercato + (1−w)·p_blend`; validation Brier
**0,2843 → 0,2818 (shrink) → 0,2771 (mercato)**. Lo shrink migliora di
**+0,25pp** ma **il mercato puro resta migliore**. Verdetto:
`model_worse_than_market`.

### 3.3 Il bias dell'EV, mercato per mercato (la parte richiesta)

| mercato | n | EV dichiarato | ROI realizzato | bias | IC95 ROI |
|---|---|---|---|---|---|
| 1X2 | 71 | +6,41% | **+7,39%** | **−1,0pp** | [−10,40, +24,25] |
| AH | 78 | +14,28% | −3,25% | **+17,5pp** | [−21,36, +13,80] |
| OU | 205 | +14,16% | −5,11% | **+19,3pp** | [−18,42, +8,09] |
| TENNIS | 40 | +5,34% | −8,99% | +14,3pp | [−36,14, +19,22] |
| **tutti** | 394 | **+11,90%** | **−2,89%** | **+14,8pp** | [−11,97, +5,96] |

**I due mercati che sbagliano di più sono OU (+19,3pp) e AH (+17,5pp)**: sono
esattamente i mercati dove la probabilità del modello è **push-aware** e la
conversione "EV → probabilità" non è l'inversa di `p·q − 1`. Su **205 righe OU**
il rendimento anticipato è +14,16% e il realizzato −5,11%.

**Il 1X2 è l'unico mercato con EV dichiarato ≈ realizzato** (bias −1,0pp) — ma
con IC95 [−10,4%, +24,3%] su 71 righe **non si può concludere nulla**: non è un
edge dimostrato, è un campione dove la varianza non è ancora stata pagata.

Nessuno di questi numeri richiede "nuovi modelli": l'EV non è corretto perché
**la probabilità è peggiore di quella di mercato** e perché **il prezzo pagato
non è quello su cui l'EV è calcolato**. Sono due difetti di misura, non di
matematica.

---

## 4. Alternative reali: cosa dice la letteratura e cosa dicono i nostri dati

Ricerca eseguita il 20/10 (fonti in calce). Confronto con l'evidenza interna.

| strategia | stato dell'arte | dati necessari | verdetto sui nostri dati |
|---|---|---|---|
| **Value betting con CLV** | il CLV positivo è il miglior predittore di profitto a lungo termine (oddsindex, unabated, oddsshopper 2025-26) | prezzi sharp + prezzo pagato | **SCARTATA CON DATI**: CLV eseguibile −3,33% |
| **Modello bayesiano gerarchico** | shrinkage empirico-Bayes per rating | storico risultati | **SCARTATA CON DATI** (già misurata nel lab): `sigma` al pavimento 0,050 → probabilità identiche alla baseline neutra; 16.259 righe su 3 campioni, 0/4 fold a favore |
| **Dixon-Coles raffinato** | correzione dei punteggi bassi | storico risultati | **RIFIUTATA DAL GATE**: rho stimato massimizza la verosimiglianza ma **peggiora il Brier 1X2** (0,5248 vs 0,5172) |
| **Steam chasing / trading sulle quote** | edge dal ritardo fra sharp e venue lenta | prezzi SX **e** sharp con timestamp | **SCARTATA CON DATI**: richiede che SX sia al livello dello sharp; il CLV −3,3% dice il contrario |
| **Arbitraggio intra-SX** | — | book bid/ask | **IMPOSSIBILE**: SX è un exchange singolo, overround interno mediano **1,75%** (misura 10/10); l'arbitraggio richiede due venue |
| **Arbitraggio cross-venue** (SX ↔ Smarkets) | profittevole in letteratura (Neely 2025, market making 2025) | **seconda venue finanziata** + book bilaterale | **BLOCCATA DA VINCOLO ESTERNO**: Smarkets non registrabile dall'Italia/richiede deposito; il misuratore `arbitrage_scan.py` esiste già (34 test) ma non ha una seconda gamba |
| **Market making su SX** | cattura lo spread (2-8 tick, letteratura 2025-26) | **book bid/ask + depth storici** | **NON VERIFICABILE OGGI**: è la lacuna dati #2 (§1). È però la sola ipotesi **coerente** col fatto che il taker paga 3-4% sopra lo sharp |

**Confronto con il benchmark di mercato**: su tutti i mercati la probabilità
devigata batte (o pareggia) la nostra. Non esiste, nei dati, un punto in cui il
nostro modello sia informativo oltre il mercato.

---

## 5. Candidati implementati e validati (copia isolata, holdout intatto)

Regola applicata: **nessun nuovo consumo dell'holdout**. I bet reali (58) sono un
campione **indipendente** dal backtest che ha consumato l'holdout, e lo split
train/validation usato è cronologico e interno a questo campione.

| candidato | come è stato valutato | risultato | esito |
|---|---|---|---|
| **A. Mercato devigato come unica probabilità** (w=1) | calibrazione + EV per costruzione | EV = 0 − vig; nessuna selezione possibile | **scartato** (non è una strategia, è l'assenza di edge) |
| **B. Blend corretto (shrink w=0,30)** | train 186 / validation 93 cronologici | Brier 0,2843 → 0,2818, ma mercato 0,2771 | **scartato**: corregge la calibrazione, **non crea edge** |
| **C. Selezione per prezzo eseguibile** (compra solo se il prezzo SX batte la fair sharp) | gate top-down già in produzione + CLV misurato | CLV −3,33%, 0 sottogruppi positivi | **scartato con dati** |
| **D. Sottoinsiemi per fascia/corsia** | 4 fasce × 2 corsie, campioni ≥ 5 | tutte negative (migliore: 1,0-1,5 −1,31%, n=8) | **scartato**: nessun sottogruppo supera il test di segno |

**Metriche registrate** per ogni candidato in `out/clv_execution.json` e
`out/ev_calibration.json`: ROI netto, CLV, IC95 bootstrap a cluster (per
partita), stabilità per finestre, numerosità, **drawdown della serie**.
Il drawdown è misurato sulla **serie reale** degli ordini eseguiti (58 campioni,
ordine cronologico): **35,68 USDC** di recupero massimo con peggior esito
singolo −5,61 USDC — su un'equity di ~40 USDC è un drawdown che esaurisce il
capitale prima che qualunque edge possa manifestarsi. Nessun candidato ha una
serie propria da simulare, perché nessuno è stato promosso.

**Nessun modello è dichiarato valido sull'unico argomento che il progetto ha già
imparato a diffidare** (un sottogruppo storico positivo): il 1X2 con ROI +7,39%
ha IC95 [−10,40, +24,25] e 71 righe, e non viene promosso.

---

## 6. Prova controllata: piano (non eseguito)

Poiché **non c'è un candidato credibile**, il piano che ha senso non è un test
LIVE ma **la costruzione del dato che può generare un candidato**. In ordine,
senza toccare CB2 né inviare ordini:

1. **Shadow a costo zero (prerequisito, 2-3 settimane)**
   - *Cosa*: log append-only del prezzo SX **con bid/ask e depth** a ogni
     lettura (già disponibile via `get_market_book`), più il prezzo sharp
     simultaneo.
   - *Perché*: senza questo il market making — la sola ipotesi coerente — è
     incommensurabile, e lo slippage segnale→esecuzione resta ignoto.
   - *Costo*: zero crediti (letture SX pubbliche), zero ordini.
2. **Criterio di promozione scritto PRIMA** (come il protocollo `prereg.py`):
   con n ≥ 100 esecuzioni, CLV eseguibile con **limite inferiore dell'IC95 > 0**
   **e** ROI netto positivo sullo stesso campione. Per distinguere un edge del
   +2% a quota 1,6 servono **~3.000 puntate**: la soglia va dimensionata sul
   numero reale di ordini/giorno, altrimenti il test non è falsificabile.
3. **Canary** (solo dopo 1 e 2): 1 ordine/giorno a stake minimo, con
   riconciliazione giornaliera e stop-loss invariati. Nessun aumento prima che
   il criterio del punto 2 sia soddisfatto su dati **nuovi**.

Non propongo test LIVE oggi perché ogni euro puntato con CLV −3,3% paga lo
spread: la prova costerebbe denaro per misurare qualcosa che i dati già dicono.

---

## 7. Limiti di questa diagnosi e passo successivo

### Limiti dichiarati (non aggirati)

1. **58 campioni eseguibili**: sufficienti per dire "negativo e significativo"
   (IC95 tutto sotto zero), **non** per stimare l'ampiezza per fascia/lega.
2. **Il +1,70% del report precedente non è ricostruibile** con i dati attuali:
   lo dichiaro invece di attribuirlo a una causa certa.
3. **Snapshot del 20/10**: non ci sono osservazioni nuove rispetto al report
   delle 13:20. Il test di "persistenza su dati nuovi" è quindi una **stabilità
   interna** (due metà, −4,50% / −2,16%), non un out-of-time vero. Un test
   out-of-time richiede un nuovo snapshot fra qualche giorno.
4. **Nessun benchmark di mercato sul backtest holdout** (0 righe): la regola
   "batte il mercato" resta **non misurabile**, non "superata".
5. **`market_quotes` senza storico**: impedisce di misurare lo slippage reale e
   il market making.
6. **Capitale**: l'equity non è ricostruibile dal ledger (depositi non
   registrati); ogni misura di staking va riferita a una lettura esterna
   timbrata.

### Cosa impedisce di verificare un vantaggio (in ordine di importanza)

1. **La venue, non il modello**: paghiamo 3-4% sopra la closing sharp.
   Qualunque modello, anche perfetto, parte da lì.
2. **Assenza del book bilaterale SX**: la strategia che potrebbe invertire il
   segno (essere *maker* invece che *taker*) non è misurabile.
3. **Campione**: 80 ordini totali non distinguono un edge del 2% (servono
   migliaia): il sistema oggi non può dimostrare **né** smentire un edge piccolo.

### Passo successivo più utile (uno solo)

**Strumentare il book SX a due lati con storico** (punto 1 del §6): è a costo
zero, non tocca il denaro, e sblocca la sola ipotesi non ancora misurata. In
parallelo, l'holdout del backtest resta intatto e nessuna soglia va toccata.

---

## Riproducibilità

```bash
venv/bin/python -m research_isolated.lab.clv_execution          # CLV eseguibile
venv/bin/python -m research_isolated.lab.ev_calibration         # calibrazione + bias
venv/bin/python -m pytest research_isolated/tests -q            # 123 test offline
```

Artefatti: `out/clv_execution.json`, `out/ev_calibration.json`,
`out/NEXT_PHASE_REPORT.md` (diagnosi precedente), `out/holdout_used.json`.
Moduli: `research_isolated/lab/clv_execution.py`,
`research_isolated/lab/ev_calibration.py` (sola lettura, zero rete, zero ordini).

**Fonti consultate (ricerca del 20/10/2026)**: oddsindex.com (CLV guide),
unabated.com ("Getting Precise About CLV", 07/2026), oddsshopper.com (06/2026),
theses-dissertations.princeton.edu (Neely 2025, arbitrage in live sports
betting), navnoorbawaresearch.com (market making in sports betting, 12/2025).
