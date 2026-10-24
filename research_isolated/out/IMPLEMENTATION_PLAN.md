# Piano di implementazione — da 21.10 USDC a una prova controllata

Questo piano parte da tre fatti misurati (`STRATEGY_RESEARCH_REPORT.md` §1):

1. il capitale è **21.10 USDC**, sotto la soglia del kill switch patrimoniale (**CB2 = 25
   USDC**) che è armato dal 18/10 04:42 — quindi **oggi il sistema non ordina**;
2. il modello **non è calibrato dove seleziona** (banda EV 10–20%: atteso +15.7%, realizzato
   −4.7% su 264 righe) e l'unica regola che migliora il campione (**tetto all'EV**) è nata
   guardando questi stessi dati, quindi va **verificata in avanti**, non creduta;
3. con 452 righe giocabili chiuse l'edge minimo distinguibile è **11–40pp**: nessuna soglia
   può essere "ottimizzata" su questo campione, e un edge del 2% richiede **≈4.400–5.500
   scommesse**. Il collo di bottiglia è **la potenza statistica e la raccolta dati**, non la
   scelta dei parametri.

Regola che governa tutto il piano: **nessun ordine live finché (a) il capitale non torna sopra
la soglia di sicurezza e (b) l'ipotesi non è pre-registrata con un endpoint misurabile adeguato
alla sua varianza.**

---

## Orizzonte A — Immediato (0–7 giorni): verifiche e raccolta dati, zero rischio

Nessuna di queste azioni tocca il denaro. Tutte sono eseguibili con l'infrastruttura attuale e
**a costo zero di crediti** (letture pubbliche SX).

### A1. Decisione sul capitale (richiede una scelta del proprietario)

| opzione | effetto | conseguenza operativa |
|---|---|---|
| **lasciare 21.10 USDC in pausa** | CB2 resta armato, 0 ordini | nessun rischio, nessun apprendimento live; il sistema continua a raccogliere segnali e telemetria |
| **top-up a ≥ 25 USDC** | CB2 si può disinnescare | 1 USDC = 4.0% del capitale per ordine: la varianza settimanale domina qualsiasi edge |
| **top-up a 100 USDC** | stake 1 USDC = **1%** | è il livello minimo per cui il tetto per ordine (12% = 12 USDC) e il ticket (1 USDC) non sono in conflitto con il rischio per puntata |

Raccomandazione: **non finanziare prima di aver letto l'esito dell'orizzonte B**. Se si finanzia
prima, si paga la varianza senza l'informazione. Il numero da tenere presente: con +2pp di edge
reale e stake 1 USDC su 21.10 USDC, il Monte Carlo dà **MaxDD p95 del 65–99%** su 500 puntate e
una probabilità del **2–18%** (secondo la quota) di restare senza capitale operativo
(`scripts/stress_test_bankroll.py --bankroll 21.10 --stake 1.0 --edge-pp 0.02`). Un edge vero
non salva un capitale che non ha granularità.

### A2. Costruire lo storico dei prezzi (il dato che manca a metà della ricerca)

Oggi `price_snapshots` contiene solo bookmaker finti e il libro SX non è archiviato: per questo
steam move, trading sulle quote e soft-book vs sharp sono `NON VERIFICABILE`. Serve:

- tabella `sx_price_history(id, fixture_id, market_type, line_key, selection, price, depth_usdc,
  observed_at, source)` con indice su `(fixture_id, market_type, selection, observed_at)`;
- un collector che, ogni **5 minuti**, legge i mercati SX in finestra (letture pubbliche,
  **zero crediti**, stesso client di `sx_signals`/`multi_market`) e appende uno snapshot
  **solo se il prezzo o la profondità sono cambiati** (dedup: senza, la tabella cresce di
  288 righe/giorno/mercato senza informazione);
- retention 90 giorni e `PRAGMA quick_check` nel backup esistente.

KPI di accettazione della raccolta: **≥ 85% dei mercati in finestra con almeno uno snapshot
ogni 15 minuti** e **≥ 30 giorni** di copertura prima di qualunque misura di steam move. Senza
questi numeri la famiglia 2 e la famiglia 6 restano non verificabili, e va detto.

### A3. Aumentare la copertura delle closing line

`closing_line.py` cattura già la chiusura Pinnacle a T-0 e ha il percorso `betfair_t0`. Oggi
la colonna del secondo benchmark è **0/45 popolata**. Obiettivo: **≥ 60% delle puntate con
entrambe le chiusure** (Pinnacle e Betfair). Costo: **1 credito per lega** alla fetch di
chiusura, dentro i budget esistenti. È il dato che rende il CLV una misura a due fonti
indipendenti, l'unica che finora mostra un segnale sopra il rumore.

### A4. Controllo tecnico unico (ripetibile, 1 minuto)

Un comando che risponde alle tre domande che contano: `CB2 armato? equity? ledger coerente?`
(esiste già come `decision status` + `execution_engine --balance` + `PRAGMA quick_check`).
Da eseguire **prima** di qualunque decisione di capitale: le soglie e i flag cambiano più
spesso di quanto si ricordi (il 15/10 `T60_WINDOW_*` era in drift di 600 minuti).

---

## Orizzonte B — Breve termine (1–4 settimane): shadow con ipotesi pre-registrata

Nessun denaro. È qui che si compra l'informazione che oggi manca.

### B1. Pre-registrazione di H1 — "tetto all'EV"

Scritta **prima** di raccogliere il campione (questo documento vale come registrazione, con la
data in testa):

- **H1**: escludere le righe con EV atteso **> 8%** migliora il CLV medio delle scommesse
  giocate rispetto a non escluderle.
- **Perché 8%**: la banda 6–10% è già negativa (−7.3%, n=60) e quella 10–20% è la più
  sovrastimata (−20.3pp). La soglia è scelta **a priori** dai bordi della griglia di
  calibrazione, non ottimizzata sul ROI; è una soglia tonda, non un punto di massimo.
- **Endpoint primario: CLV** (non il ROI). Motivo quantitativo: il CLV ha una deviazione
  standard per scommessa di 1–3 punti, il ROI di 40–100. Per distinguere 1 punto di CLV
  servono ~**70–150** osservazioni; per distinguere 2 punti di ROI ne servono **~4.000**.
  Misurare il ROI come endpoint primario significa non poter concludere nulla (è esattamente
  l'errore che il progetto ha già fatto tre volte).
- **Endpoint secondario: ROI** delle due coorti (esclusi vs inclusi), dichiarato come
  descrittivo, **non** come criterio di decisione.
- **Campione**: 150 righe giocabili chiuse con EV>8% **e** 150 con EV≤8%, oppure 60 giorni,
  quello che arriva prima.
- **Test**: confronto della media CLV fra coorti (Welch), più bootstrap a cluster per partita;
  α = 0.05, un solo test → nessuna correzione necessaria (a differenza della griglia del lab,
  dove i test erano 6).
- **Criterio di successo**: CLV medio della coorte esclusa **inferiore** a quello della coorte
  inclusa con p < 0.05 e differenza ≥ 1 punto percentuale. Se il CLV delle due coorti è
  uguale, H1 è **falsificata** e il tetto all'EV non va in produzione (il miglioramento del
  ROI visto in-sample era rumore).
- **Criterio di fallimento anticipato**: se dopo 60 giorni una delle due coorti ha < 60
  osservazioni, la conclusione è `NON VERIFICABILE` e si dichiara tale (non si abbassa la
  soglia per arrivare a un numero).

### B2. Modalità shadow delle due ipotesi (zero denaro)

- registrare su un JSONL, per ogni segnale prodotto dalle corsie, **quale stake sarebbe stato
  ordinato** e **quale sarebbe stato escluso** da H1, con la quota di esecuzione osservata;
- misurare **fill rate per pick** (la telemetria `fill_rate.py` esiste dal 17/10) e **slippage**
  fra prezzo atteso e prezzo di riempimento: senza questi due numeri un edge di 1–2% è
  indistinguibile dai costi;
- nessuna scrittura sulla tabella `bets`, nessuna chiamata a `_live_fill`.

### B3. Cosa NON fare in questa fase

- non toccare le soglie (EV, edge, fascia) per "far girare il volume": il lab ha mostrato che
  il vincolo è la potenza, non la soglia;
- non scegliere la soglia guardando l'holdout: l'holdout del lab è già stato consumato una volta
  (l'ipotesi H1 è nata lì) e non va riusato;
- non aumentare il numero di mercati prima di poterli misurare: OU/AH sono oggi **non
  eseguibili** (griglia SX a passi di 0,5 contro la main line di Pinnacle) e restano telemetria.

---

## Orizzonte C — Dopo la validazione: prova controllata con stake limitato

Si attiva **solo** se B1 ha dato esito positivo e il capitale è stato riportato a un livello
compatibile con la granularità del rischio.

### C1. Condizioni di ingresso alla prova

| condizione | soglia |
|---|---|
| capitale | **≥ 100 USDC** (1 USDC = 1% del capitale) |
| H1 | endpoint primario soddisfatto (CLV coorte esclusa < inclusa, p < 0.05, Δ ≥ 1pp) |
| CLV di processo | **> 0** su ≥ 100 campioni non-eco |
| fill rate per pick | **≥ 80%** (misurato, non stimato) |
| slippage medio | **≤ 0.5%** del prezzo richiesto |
| guardrail | tutti attivi e non manomessi (CB2, stop giornaliero 5%, settimanale 12%, recinto) |

### C2. Dimensionamento della prova

- stake **1 USDC** fisso per ordine (non Kelly: in prova la size non è la variabile);
- massimo **3 ordini aperti**, massimo **2 ordini/giorno**, massimo **20 ordini/settimana**;
- nessuna leva, nessun deposito aggiuntivo durante la prova;
- durata minima: **100 ordini** o 8 settimane.

### C3. Criteri di arresto automatici (non discrezionali)

- CB2 (equity ≤ 25 USDC) — invariato;
- stop giornaliero 5% e settimanale 12% — invariati;
- **nuovo**: stop della prova se il CLV rolling a 30 campioni scende sotto **−1%** (il processo
  si è rotto, non è sfortuna);
- **nuovo**: stop della prova se il fill rate per pick scende sotto il 60% (i costi di
  esecuzione hanno mangiato l'edge);
- rientro: si riprende solo dopo una causa identificata e documentata, mai "per recuperare".

### C4. Criterio di promozione a size piena

ROI positivo **e** CLV positivo **e** nessuna violazione di guardrail su ≥ 100 ordini, con
intervallo di confidenza del ROI che esclude lo zero. Con 100 ordinarie e questo profilo di
rischio l'edge minimo distinguibile è ~20pp: la promozione a size piena richiede quindi **una
prova di processo (CLV, fill, slippage)**, non una prova di ROI. Il ROI si conferma solo con
campioni che oggi non esistono.

---

## Cosa serve per sbloccare le famiglie oggi `NON VERIFICABILE`

| famiglia | dato mancante | come si ottiene | costo |
|---|---|---|---|
| soft book vs sharp | quote di più bookmaker sullo **stesso** evento a orari diversi | rotazione quote su `regions=eu` già paga i bookmaker: serve **salvarli tutti**, oggi si usano solo gli sharp | 0 (già pagato) |
| steam move | serie temporale dei prezzi sharp e SX | A2 + `price_snapshots` alimentata anche dagli sharp | 0 |
| trading sulle quote | storico prezzi + commissioni + fill | A2 e misure di fill/slippage di B2 | 0 |
| arbitraggio cross-venue | un secondo conto finanziato (Smarkets) | non disponibile: i vincoli dichiarati escludono depositi presso terzi | — |

---

## Cronologia

| quando | cosa | rischio |
|---|---|---|
| **oggi** | nessun ordine; CB2 armato; report consegnato | zero |
| settimana 1 | A2 collector prezzi, A3 copertura chiusure, A4 controllo tecnico | zero |
| settimane 2–6 | B1/B2 in shadow, campione 150+150, lettura CLV | zero (nessun ordine) |
| dopo B1 | decisione sul capitale e, solo se positiva, prova C con 1 USDC | limitato e prestabilito |

## Dichiarazione finale

Non esiste, nei dati disponibili, un'opportunità verificata da eseguire stanotte. Il sistema è
fermo per un blocco di sicurezza che è coerente con l'evidenza (nessun edge dimostrato, capitale
sotto soglia). Le due cose che hanno valore economico oggi sono **raccogliere i dati che
mancano** e **testare in avanti l'unica ipotesi sopravvissuta**; entrambe costano zero crediti e
zero rischio di capitale.
