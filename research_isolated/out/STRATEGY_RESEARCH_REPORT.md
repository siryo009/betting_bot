# QUOTAVERACE — Ricerca di un vantaggio economico reale

**Data:** 20/10/2026 · **Perimetro:** copia isolata `research_isolated/` (sola lettura sullo
snapshot di produzione, nessun ordine, nessun deploy) · **Capitale misurato:** 21.1004 USDC
liberi, 0 in escrow (`execution_engine.py --provider sxbet --balance`, 03:25 UTC)

Regola con cui è stato scritto questo documento: **nessun numero senza campione e intervallo**,
**nessuna strategia dichiarata valida su un ROI scelto a posteriori**, e ciò che i dati non
permettono di misurare viene marcato `NON VERIFICABILE` invece di essere stimato.

---

## 0. Sintesi per il decisore

1. **Il sistema non sta perdendo per colpa delle soglie: è fermo.** Il kill switch patrimoniale
   (CB2, soglia 25 USDC) è scattato il **18/10 alle 04:42** con equity 23.69 e da allora non
   parte un ordine. Oggi il capitale è **21.10 USDC**: sotto la soglia, il blocco resta armato.
2. **La stima di valore (EV) del modello non è calibrata dove il sistema punta.** Sulle 452
   righe giocabili chiuse l'EV atteso era **+11.53%** e il realizzato **−2.52%**; nella fascia
   di EV 10–20% (264 righe, il 58% del campione) l'atteso era **+15.7%** contro **−4.7%**
   realizzato. La correlazione di rango fra EV e risultato è **−0.03** (CI95 −0.14…+0.08):
   l'EV, così com'è, non ordina i risultati.
3. **Nessuna combinazione ex ante sopravvive alla correzione per test multipli**: 6 celle
   (mercato × fascia quota × fascia edge) con n≥25, **0 sopravvissute** a Bonferroni.
4. **Il campione non può dimostrare un edge del 2%.** Con 78–452 righe per strategia l'edge
   minimo distinguibile è **11–40 punti percentuali**; per un edge del 2% servono **≈4.400–5.500
   scommesse** su questo profilo di rischio (`required_bets_for_2pct` per strategia). Il problema
   non è "trovare la soglia giusta".
5. **L'unica misura di processo sopra il rumore è il CLV**: **+0.52%** su 448 campioni
   (CI95 +0.25…+0.80), **+1.70%** sui 137 non-eco, **+3.34%** sui 40 misurati contro la closing
   di Pinnacle (CI95 +1.90…+4.88). Ma il P/L realizzato resta **−7.39%** e con 21 USDC non c'è
   modo di distinguere "processo buono, varianza sfortunata" da "processo buono solo sulla carta".

**Decisione:** nessuna strategia è `VALIDATO FUORI CAMPIONE`. Due candidati sono `PROMETTENTE`
(tetto all'EV; CLV come criterio di processo), tutti gli altri `DA VERIFICARE`, `SCARTATO` o
`NON VERIFICABILE`. **Nessun ordine live è stato forzato**: con capitale sotto il floor
patrimoniale e nessun edge dimostrato non esiste un'opportunità verificata da eseguire.

---

## 1. Cosa ho trovato, in ordine di importanza economica

### 1.1 Il vincolo operativo è il capitale, non il segnale

| grandezza | valore misurato | fonte |
|---|---|---|
| capitale libero SX | **21.1004 USDC** (escrow 0) | `execution_engine --balance`, 20/10 03:25 UTC |
| CB2 (kill switch patrimoniale) | **armato** dal 18/10 04:42, equity 23.6901 ≤ 25.00 | `data/execution/t60_kill.json` |
| ultima puntata reale | **18/10 04:01:41** | ledger `bets` |
| ordine minimo exchange | **1.00 USDC** | SX Bet |
| ticket minimo del motore | **1.00 USDC** | `decision.stake_engine` |
| cap per ordine | 12% = **2.53 USDC** | `KELLY_MAX_STAKE_PCT` |
| recinto esposizione aperta | 70% = **14.77 USDC** | `OPEN_EXPOSURE_CAP_PCT` |

Con 21.10 USDC ogni puntata da 1 USDC è **4.7% del capitale** e il tetto per ordine (2.53) è
**12%**: la granularità del rischio è grossolana per costruzione. Il capitale è 3.90 USDC sotto
la soglia che il sistema stesso considera "non più sostenibile".

### 1.2 L'EV è gonfiato proprio dove il sistema seleziona

Righe giocabili chiuse (una per partita/esito, `created_at` crescente):

| fascia di EV atteso | n | EV atteso | ROI realizzato | scarto |
|---|---|---|---|---|
| 0–2% | 24 | +1.8% | **+11.2%** | +9.5pp |
| 2–4% | 69 | +3.0% | **+2.6%** | −0.4pp |
| 4–6% | 30 | +4.8% | +12.9% | +8.0pp |
| 6–10% | 60 | +8.1% | **−7.3%** | −15.4pp |
| **10–20%** | **264** | **+15.7%** | **−4.7%** | **−20.3pp** |

La banda 2–4% è calibrata quasi perfettamente; sopra il 6% l'EV è **sistematicamente
sovrastimato**. Non è un problema di soglia minima (alzarla seleziona edge ancora più gonfi):
è un problema di **stima**. Le probabilità del modello sono sovraconfidenti nella parte alta
(0.65–0.70: previsto 0.669, osservato **0.558**, −11.1pp).

### 1.3 Nessuna cella ex ante sopravvive ai test multipli

Griglia definita **prima** di guardare i risultati (4 mercati × 6 fasce quota × 5 fasce edge,
n≥25): 6 celle testate, α di Bonferroni = 0.0083, **0 sopravvissute**. La migliore cella grezza
(OU quota 1.40–1.60, edge ≥ 10pp, n=40, ROI +6.5%) ha p = 0.54.

Conseguenza: qualunque "strategia vincente" individuata scegliendo una cella su questo campione
è rumore. È la stessa lezione che il progetto ha già pagato due volte (22/09 e 25/09).

### 1.4 Il collo di bottiglia misurato (non ipotizzato)

| stadio | valore | lettura |
|---|---|---|
| righe di ledger scansionate | 2.200 | — |
| righe decise (con esito) | 2.127 | — |
| righe giocabili | 480 (22.6% delle decise) | il gate seleziona poco, ed è voluto |
| giocabili chiuse | 452 | campione della misura |
| puntate reali | **80** | 0.18 puntate per riga giocabile chiusa |
| ROI giocabili (per unità) | **−2.52%** | atteso +11.53% |
| ROI puntate reali | **−7.39%** (CI95 −31.1…+7.1) | 40 vinte / 40 perse |

Il "buco" fra ledger e corsia reale è dichiarato e misurato: **52 delle 80 puntate** (65%)
trovano una riga giocabile corrispondente, con **EV medio 6.56%**. Le puntate reali non sono
una selezione a caso del ledger — e infatti hanno un prezzo medio di **2.07** (1X2) e **2.48**
(tennis), cioè **fuori** dalla fascia attuale 1.20–2.00 in larga parte: sono in buona parte
dell'era precedente ai filtri di oggi.

### 1.5 Dove si perdono i soldi veri (fasce di prezzo pagato)

| fascia di prezzo pagato | n (partite) | ROI | CI95 (bootstrap a cluster) | vinte |
|---|---|---|---|---|
| 1.20–1.40 | 6 (6) | **+33.0%** | +31.1…+35.6 | 6/6 |
| 1.40–1.60 | 11 (11) | +3.2% | −47.3…+36.2 | 7/11 |
| 1.60–1.80 | 18 (18) | **−18.2%** | −59.4…+21.9 | 9/18 |
| 1.80–2.00 | 19 (19) | +3.4% | −40.0…+50.1 | 11/19 |
| 2.00–2.50 | 16 (16) | −13.4% | −67.3…+42.3 | 6/16 |
| **2.50+** | **10 (8)** | **−67.0%** | −100.1…**+1.2** | **1/10** |

**Correzione dichiarata**: la prima stesura di questa tabella usava un bootstrap sui singoli
gettoni e dava per la fascia 2.50+ un CI −100.1…−16.7 («statisticamente conclusivo»). Le
puntate della stessa partita condividono esito e prezzo, quindi l'unità di indipendenza è la
**partita**: con il bootstrap a cluster (8 partite) il limite superiore è **+1.2%** e l'intervallo
**include lo zero**. La fascia resta la più negativa del dataset (1 vinta su 10) ma **non è
conclusiva** — e va detto prima che qualcuno costruisca una regola sopra.

Ne segue un fatto più importante: **nessuna popolazione di questo dataset ha insieme n ≥ 30 e
CI che esclude lo zero.** Il dataset non contiene alcun risultato statisticamente conclusivo,
né in positivo né in negativo. La stretta sulla fascia tennis (2.30) resta coerente con questi
numeri, ma è una scelta di prudenza sostenuta da un quadro descrittivo, non da una prova.

### 1.6 CLV: il processo è (debolmente) positivo, il conto no

| popolazione | n | CLV medio | CI95 | quota positiva |
|---|---|---|---|---|
| tutte le chiusure | 448 | **+0.52%** | +0.25…+0.80 | 19.4% (mediana 0 = eco) |
| non-eco | 137 | **+1.70%** | — | 63.5% |
| closing **Pinnacle** (T-0) | 40 | **+3.34%** | +1.90…+4.88 | — |
| closing SX | 9 | **−0.53%** | −0.95…−0.13 | — |

Sui **71** campioni non-eco agganciabili a una riga giocabile, la coorte a **CLV positivo**
(n=53) ha reso **+10.5%** per unità (CI95 −10.3…+30.9) e quella a **CLV negativo** (n=18)
**−47.9%** (CI95 −80.9…−10.2): la relazione esiste ed è quella attesa dalla teoria (il CLV è
il predittore standard dell'edge), ma nessuna delle due coorti raggiunge 30 righe e il P/L
aggregato resta negativo: non è una prova. Inoltre la coorte "Pinnacle T-0" (la più pulita,
+2.92% di CLV) ha realizzato **−6.62%** su 29 righe: a questo n il CLV non si traduce in soldi.

---

## 2. Metodo (dichiarato prima dei numeri)

- **Snapshot**: copia del DB di produzione in `research_isolated/snapshot/quotaverace_prod.db`,
  aperta in `mode=ro` (una `UPDATE` viene **rifiutata** dal driver: test dedicato).
- **Nessun look-ahead**: le regole usano solo quota, probabilità, EV, edge, mercato, lega,
  `created_at`. Prova anti-leakage per **sign flip** (esiti invertiti, `profit` negato): la
  selezione deve restare identica. Una regola che guarda l'esito viene **rilevata** (test).
- **Split cronologico calcolato sull'universo della strategia**, non sul dataset: il primo
  tentativo tagliava il dataset intero e dava una validation da 15 righe per il 1X2 — una misura
  del calendario, non della strategia. Con il taglio corretto: 56 validation / 22 holdout.
- **Bootstrap a cluster** (per partita) su ogni ROI e su ogni CLV; **potenza dichiarata**
  (edge minimo distinguibile) per ogni campione.
- **Test multipli**: Bonferroni sulla griglia ex ante, numero di test dichiarato.
- **Classificazione**: `NON VERIFICABILE` (n=0) · `DA VERIFICARE` (n<30 o CI che include 0) ·
  `PROMETTENTE` (n≥30 e CI interamente > 0) · `SCARTATO` (n≥30 e CI interamente < 0).
  `VALIDATO FUORI CAMPIONE` non è assegnato a nessuna strategia in questo documento: perché lo
  sia serve una prova **pre-registrata** su dati che non hanno contribuito a sceglierla.

---

## 3. Rassegna delle 8 famiglie (cosa dicono le fonti e cosa dicono i nostri dati)

| # | famiglia | stato | evidenza esterna | evidenza sui nostri dati |
|---|---|---|---|---|
| 1 | **Value betting con probabilità sharp** | `PROMETTENTE` (condizionata) | Battere la closing line è presentato come il predittore standard dell'edge da tutte le fonti consultate (oddsindex, Unabated, thepowerrank, sharpfootballanalysis — vedi `SOURCES.md`). | CLV vs Pinnacle **+3.34%** (n=40) ma P/L **negativo**; EV del modello sovrastimato sopra il 6% |
| 2 | **CLV e movimenti di quota** | `PROMETTENTE` come metrica, `SCARTATO` come segnale operativo | CLV come metrica di processo: posizione concorde delle fonti consultate; nessuna di esse porta una prova riproducibile di profitto da steam move su un exchange calcistico come SX. | `price_snapshots` contiene **solo bookmaker finti BookA/BookB**: nessuna serie di prezzi reali → `NON VERIFICABILE` per lo steam move |
| 3 | **Modelli statistici indipendenti** (Dixon-Coles, bayesiano, rating) | `SCARTATO` allo stato attuale | Dixon & Coles 1997 è il riferimento; i modelli di rating battono il mercato solo con informazioni che il mercato non ha. | `dixon_coles` (17/10): ρ stimato −0.28 ma il gate di calibrazione lo **rifiuta** (Brier holdout peggiore). `bayes_ratings`: **non** batte il rating attuale (σ inchiodato al pavimento). `boosting` vs logistica: **peggiore** in modo significativo |
| 4 | **Machine learning** | `SCARTATO` (per ora) | Hubáček, Šourek, Železný (2019, *Int. J. Forecasting*): il vantaggio documentato viene dalle **feature** (informazione che il mercato non ha), non dall'algoritmo. La rassegna arXiv 2410.21484 conferma che i risultati pubblicati sono fragili fuori campione. | Walk-forward 4 fold, 15.213 righe out-of-fold: logistica Brier **0.2344** / AUC 0.632; boosting **0.2395** / 0.6135 → boosting **peggiore** (p=0.0001) |
| 5 | **Arbitraggio cross-venue** | `SCARTATO` | Esiste fra book e prediction market (Steinmetz 2026), richiede due conti finanziati e latenza. | Misura 10/10 su 98 partite SX: overround interno mediano **1.75%**, **0** coperture < 1.0; Smarkets non attivabile → non realizzabile |
| 6 | **Trading sulle quote** (entrate/uscite) | `NON VERIFICABILE` | Il confronto finanza/gambling (Liu 2026, Springer) descrive il trading come fenomeno reale ma dipendente da microstruttura e costi; servono storico dei prezzi, commissioni e fill. | Nessuno storico dei prezzi SX: non esiste un dataset per simulare ingressi/uscite. `price_snapshots` è vuoto di prezzi reali |
| 7 | **Mercati alternativi** (OU, AH, tennis) | `DA VERIFICARE` / `SCARTATO` | Gli handicap e i totali sono i mercati in cui i modelli indipendenti hanno più margine (letteratura sui totali). | OU: full −2.9% (n=247), holdout **−13.4%**; AH: full −0.7% (n=92); tennis: full −3.6% (n=54) ma **CLV +4.24%** (n=40). Nessuno eseguibile oggi (oracolo a linea spento, griglia SX/Pinnacle che non si interseca) |
| 8 | **Non-betting** | `SCARTATO` per questo capitale | — | 21 USDC a un rendimento risk-free del 4%/anno producono **0.84 USDC/anno**: non è un orizzonte economico. Le alternative automatizzate ad alto rendimento richiedono rischio di perdita totale, leva o depositi presso terzi, esclusi dai vincoli |

### 3.1 Cosa manca per rendere verificabili le famiglie 2 e 6

Il difetto comune è **la mancanza di uno storico dei prezzi**. Oggi:
`price_snapshots` contiene 2 bookmaker finti; il libro SX non viene archiviato; le quote sharp
arrivano dalla cache con TTL e vengono sovrascritte. Per una strategia di movimento di quota o
di trading serve una **serie temporale** con: timestamp, venue, esito, prezzo, profondità.
Questa è la prima cosa da costruire (piano, orizzonte A) e **non richiede crediti** per il lato
SX (letture pubbliche).

---

## 4. Gradutatoria quantitativa (dettaglio in `STRATEGY_COMPARISON.csv`)

| strategia | n (full) | ROI full | CI95 | holdout | edge minimo distinguibile | classe |
|---|---|---|---|---|---|---|
| **value_capped_ev** (tetto EV 10%) | 59 | **+19.6%** | **+1.6…+36.2** | +36.6% (n=17) | 25.8pp | `PROMETTENTE` (post-hoc, da pre-registrare) |
| value_capped_ev_tight (tetto 6%) | 48 | +21.4% | +1.0…+40.4 | +33.2% (n=16) | 28.3pp | `PROMETTENTE` (idem) |
| value_1x2 (produzione) | 78 | +11.4% | −5.2…+28.0 | +28.6% (n=22) | 24.0pp | `DA VERIFICARE` |
| value_tight_1x2 (1.60–1.80) | 27 | +18.7% | −10.3…+45.0 | +13.0% (n=3) | 40.5pp | `DA VERIFICARE` |
| market_only_favourite (senza modello) | 310 | +1.5% | −6.2…+9.0 | −2.3% (n=66) | 10.8pp | `DA VERIFICARE` (baseline) |
| ou_gate | 247 | −2.9% | −14.9…+8.6 | −13.4% (n=103) | 12.1pp | `DA VERIFICARE` |
| ah_gate | 92 | −0.7% | −16.9…+15.3 | −3.2% (n=45) | 20.8pp | `DA VERIFICARE` |
| tennis_band | 54 | −3.6% | −21.2…+13.9 | −5.1% (n=20) | 25.6pp | `DA VERIFICARE` |
| contrarian_ou_rejected (contrasto) | 803 | +5.3% | −9.6…+21.4 | +5.6% (n=207) | 19.0pp | `DA VERIFICARE` (non giocabile) |
| grid 1X2 (walk-forward) | 16 | +5.5% | — | — | — | `DA VERIFICARE` |
| soft book vs sharp | — | — | — | — | — | `NON VERIFICABILE` |

**Come leggere le due righe verdi.** `value_capped_ev` ha CI>0 sia in full (59 righe) sia in
holdout (17 righe: +36.6%, CI +8.1…+58.1), **ma** l'holdout ha 17 righe — sotto la soglia di 30
che il progetto richiede per concludere, e il CSV infatti marca solo il *full* come
`PROMETTENTE` e gli split come `DA VERIFICARE`. Soprattutto: **l'ipotesi è nata guardando la
stessa tabella di calibrazione** (banda EV 2–6% calibrata, 10–20% sovrastimata), quindi è una
**ipotesi generata in-sample**, non una scoperta fuori campione. Il segno è coerente, il
meccanismo è plausibile, il campione non basta: per questo è `PROMETTENTE` e non `VALIDATO`.

La baseline senza modello (+1.5%, CI che include lo zero) dice che **l'apporto del modello non è
dimostrato**: il modello aggiunge selezione (78 righe contro 310) senza aggiungere rendimento
misurabile.

---

## 5. Cosa è stato implementato (e con quali garanzie)

```
research_isolated/
  lab/loader.py        snapshot read-only, audit integrità
  lab/stats.py         CI bootstrap a cluster, Wilson, potenza, classificazione
  lab/strategies.py    regole ex ante (soglie LETTE da value_filter/market_calib/tennis_lane)
  lab/backtest.py      split cronologico per universo, griglia, walk-forward, potenza
  lab/soft_book.py     soft book vs sharp (nessuna osservazione disponibile → NON VERIFICABILE)
  lab/diagnostics.py   corsia vs ledger, fasce, calibrazione EV, monotonicità, celle+Bonferroni
  lab/run_lab.py       giro completo → JSON/CSV/log
test_research_lab.py   49 test, tutti offline
```

Garanzie verificate dai test:
- **sola lettura**: la connessione rifiuta `CREATE`/`UPDATE`; nessun `INSERT/UPDATE/DELETE` nel
  sorgente; nessun import di rete nei moduli del lab;
- **anti-leakage**: sign-flip → selezione identica per tutti i candidati; due regole sporche di
  prova vengono **rilevate**; il tripwire legge il **codice** (tokenizzato), non la prosa;
- **niente seconda verità**: le soglie dei candidati sono confrontate con `value_filter`,
  `market_calib` e `tennis_lane` (test dedicati: fascia quota, EV minimo, edge minimo, fascia
  tennis, OU/AH);
- **riproducibilità**: `venv/bin/python -m research_isolated.lab.run_lab` rigenera JSON, CSV e
  log da uno snapshot dichiarato.

Un **errore reale del lab, trovato e corretto**: il primo `leak_audit` permutava le colonne di
esito, cambiando la *popolazione* delle righe chiuse; tutte le regole risultavano "sporche" per
un motivo che col leakage non c'entra. La prova corretta è il sign-flip. Simmetricamente, il
primo tripwire testuale bocciava la parola "profittevoli" in una docstring: ora guarda i token.

---

## 6. Decisione finale

### Evidenze favorevoli a un edge
- **CLV positivo e statisticamente non nullo** su tutti i campioni non-eco (+1.70%, 63.5%
  positivi) e **+3.34%** contro la closing Pinnacle (n=40, CI interamente > 0).
- La coorte a CLV positivo ha reso **+10.5%** (n=53) contro **−47.9%** (n=18) di quella a CLV negativo.
- La banda di EV **2–4% è calibrata** (atteso 3.0% / realizzato 2.6%): il modello *sa* stimare
  quando non pretende troppo.
- Esiste una regola semplice e con meccanismo chiaro (**tetto all'EV**) che migliora il campione
  in modo coerente in validation e holdout.

### Evidenze contrarie
- **P/L reale negativo** su 80 puntate (−15.39 USDC, −7.39%) e su 452 righe giocabili (−2.52%).
- **EV non monotono** rispetto al risultato (−0.03) e **sovrastimato di 20pp** nella banda dove
  il sistema seleziona di più.
- **0 celle** sopravvivono ai test multipli; **nessuna** strategia può essere misurata con la
  potenza necessaria ad affermare un edge del 2%.
- **Nessuna popolazione** ha n ≥ 30 con CI che esclude lo zero: il dataset non produce alcuna conclusione statistica, né favorevole né contraria. La fascia peggiore (> 2.50: −67%, 1 vinta su 10) ha 8 partite.
- Il capitale (21.10 USDC) è **sotto** la soglia che il sistema stesso considera sostenibile, e
  con stake minimo 1 USDC il rischio per puntata è 4.7%: il *rumore* settimanale è più grande
  dell'edge annuale ipotizzabile.

### Conclusione operativa
**Non forzare ordini.** Il blocco CB2 in vigore è coerente con l'evidenza: senza un edge
dimostrato, l'esposizione al rischio non è compensata. Le due cose che hanno valore economico
sono (a) **raccogliere i dati che oggi non esistono** (storico dei prezzi e delle chiusure
sharp) e (b) **pre-registrare** l'unica ipotesi sopravvissuta (tetto all'EV) e testarla in
avanti. Il piano è in `IMPLEMENTATION_PLAN.md`.

---

## 7. Come riprodurre

```bash
venv/bin/python -m research_isolated.lab.run_lab          # JSON + CSV + log
venv/bin/python -m pytest test_research_lab.py -q         # 49 test, offline
venv/bin/python scripts/stress_test_bankroll.py --sims 20000 --bets 500 \
    --bankroll 21.10 --edge-pp 0.02 --json                 # rischio del capitale
```

Lo snapshot è una copia del DB di produzione del 20/10/2026; per rifarla:
`railway ssh --service betting_bot -- python3 -c "import sqlite3;..."` (procedura in
`research_isolated/README.md` se presente) oppure copia del file dal volume. Il laboratorio
**non inventa dati**: se lo snapshot manca, `loader.require_snapshot()` solleva.

---

## 8. Limiti dichiarati

1. **Ampiezza del campione**: 80 puntate reali, 452 righe giocabili chiuse, 40 chiusure Pinnacle.
   Tutte le conclusioni "positive" hanno intervalli larghi; quelle "negative" sono più solide
   perché il campione è più grande (803 righe per il contrasto OU).
2. **Selezione dell'era**: il ledger contiene righe dal 01/10, con soglie cambiate più volte
   (fascia 1.20–2.00 dall'8/10, tennis 2.30 dal 14/10, EV 1.5% dal 14/10). Il filtro d'era è
   applicato dove serve (`created_at` nello split) ma non può correggere il fatto che le righe
   vecchie sono state generate da una strategia diversa.
3. **Nessuno storico dei prezzi SX**: soft book vs sharp, steam move e trading sulle quote non
   sono misurabili. Non ho simulato prezzi eseguibili inesistenti.
4. **Il CLV non è un P/L**: a 40 campioni non prova un edge economico, solo un processo
   plausibilmente corretto.
5. **Costi non modellati**: commissioni/gas SX, slippage oltre la profondità misurata, tempi di
   settlement. Il fill rate reale per pick è 100% su una finestra di 26 minuti di log: troppo
   poco per una stima.
