# QUOTAVERACE — Diagnosi del segnale e decisione operativa

**Data:** 20/10/2026 23:19 UTC · **Perimetro:** copia isolata `research_isolated/` · snapshot `quotaverace_prod.db` (sola lettura) · **nessun ordine, nessuna modifica alla produzione**

Regola: ogni numero ha campione e intervallo; ciò che i dati non permettono di misurare è marcato `NON VERIFICABILE`.

---

## 0. Sintesi per il decisore

1. **Capitale riconciliato: 22.9404 USDC** (RICONCILIATO; residuo -0.0397 USDC). Il valore dichiarato è il **disponibile**, l'escrow al momento della lettura è 0.0000 USDC, quindi l'equity di riferimento è 22.9404 USDC: nessun denaro in gioco fuori dal conteggio.
2. **Il divario atteso/realizzato ha una causa contabile precisa, non la sfortuna**: 45% del divario è una **definizione diversa** dello stesso numero. L'EV a ledger (+11.68%) è calcolato sulla probabilità del **modello**, mentre `prob` è la probabilità **blendata col mercato**; tolti i +6.78% di definizione l'EV riproducibile è +4.90%. Il realizzato (-3.42%) sta 2.26% sotto il mercato e 15.10% sotto il modello — ma il suo IC95 è -13.06%…+6.36%: il campione **smentisce l'EV del modello**, non dimostra che il mercato sia calibrato.
3. **La componente statistica è grande e va dichiarata**: IC95 del realizzato -13.06%…+6.36%, edge minimo distinguibile +11.24% con 455 chiusure.
4. **Il CLV positivo è il margine del bookmaker**: +2.70% contro la quota sharp, **-1.35% dopo il de-vig** (margine misurato +4.11%, 1.50× il CLV misurato, n=34). Controllo indipendente: contro la **stessa venue** (SX) il CLV è -0.53% (n=9).
5. **Test pre-registrato: INCONCLUSIVE** — IC95 contiene lo zero. Candidato: -4.76% (n=179), benchmark +6.41% (n=60).

**DECISIONE: NESSUN VANTAGGIO DIMOSTRATO**

- holdout INCONCLUSIVE: IC95 contiene lo zero

CB2 **armato**, LIVE **disabilitato**, nessun ordine eseguito.

---

## 1. Riconciliazione del capitale

| grandezza | valore | fonte |
|---|---|---|
| saldo dichiarato | **21.1004 USDC** | execution_engine.py --provider sxbet --balance (availableBalance) @ 2026-10-20T03:25:00+00:00 |
| escrow al momento della lettura | 0.0000 USDC | ledger `bets` (ordini aperti) |
| equity di riferimento | **22.9404 USDC** | disponibile + esposizione |
| ancora esterna | 23.6901 USDC @ 2026-10-18T04:42:00+00:00 | data/execution/t60_kill.json (equity all'innesco del CB2) |
| puntate live a ledger | 81 | stake 209.98 USDC, P/L -15.39 USDC |
| ordini aperti | 1 | stake 1.8400 USDC |

**Il conto della riconciliazione** (dall'ancora al saldo dichiarato):

```
equity(ancora)                       23.6901
- esposizione aperta(ancora)         12.1000
+ cassa rientrata (5 settlement)  9.5486
+ esposizione aperta(oggi)           0.0000
= atteso                             21.1401
  dichiarato                         21.1004
  residuo                            -0.0397 USDC (-0.19%)
```

Esito: **RICONCILIATO** · tolleranza 0.05 USDC · `stale`: False · settlement dopo la misura: 0

Cosa questo NON dimostra: il **livello** del capitale non è ricostruibile dal ledger, perché i depositi non sono movimenti registrati. La verifica riguarda i **movimenti** dal 18/10 04:42: da lì in poi ogni centesimo è spiegato da vincite, push e perdite.

## 2. Perché l'EV atteso non coincide con i risultati

### 2.0 Da dove vengono i due numeri citati (+11,53% e −2,52%)

La coppia citata **non è un errore di contabilità**: sono le due medie sullo **stesso** insieme — tutte le righe giocabili chiuse, scadenze incluse. La verifica è la tabella qui sotto.

| popolazione | n | EV a ledger | ROI realizzato | divario | vinte / perse / push |
|---|---|---|---|---|---|
| tutte le chiuse (la coppia citata) | 455 | +11.49% | -2.83% | +14.31% | 234 / 142 / 79 |
| puntate effettive (scadenze escluse) | 399 | +11.80% | -3.22% | +15.02% | 234 / 142 / 23 |
| solo decise (won/lost) | 376 | +11.68% | -3.42% | +15.10% | 234 / 142 / 0 |

**+11.49% atteso e -2.83% realizzato sullo stesso campione** (n=455), divario +14.31%. Sulle sole 376 decise il divario sale a +15.10%.

Il controllo contabile, voce per voce — nessuna delle seguenti spiega il grosso del divario:

- **commissioni: nessuna**. SX Bet è un exchange P2P e il ledger registra `profit` **per unità di stake** senza fee; il costo del referto è in crediti API, non in cassa, quindi non entra nel P/L.
- **push e rimborsi**: 79 chiuse come push (56 scadenze + 23 rimborsi di mercato veri) con profitto 0: **diluiscono** la media (+1.14% sulle chiuse), non la falsano.
- **duplicati**: 0 righe rimosse dal dedup partita+esito · **verdetti contraddittori**: 0.
- **risultati mancanti**: le 56 scadenze (12.3% delle chiuse) sono righe mai giocate, chiuse come push perché il referto non ha trovato il risultato (scadenza a 5 giorni): contano zero.

### 2.1 Le due grandezze confrontate non sono la stessa cosa

| mercato | n | EV a ledger | `prob*quota-1` | scarto | righe coerenti |
|---|---|---|---|---|---|
| 1X2 | 74 | +6.60% | +6.60% | +0.00% | 100.0% |
| AH | 81 | +14.35% | +4.33% | +10.02% | 1.2% |
| OU | 220 | +14.07% | +4.35% | +9.72% | 2.3% |
| TENNIS | 80 | +6.02% | +6.02% | -0.00% | 100.0% |

Coerenza piena (100%) su **1X2, TENNIS**: lì l'EV a ledger è esattamente `prob*quota-1`, quindi il numero descrive la probabilità registrata. Non lo è su **AH** (scarto medio +10.02%, coerenza 1.2%), **OU** (scarto medio +9.72%, coerenza 2.3%): qui l'EV è `p_win*(quota-1) - p_lose` sulla probabilità **del modello** (push che restituisce lo stake) mentre `prob` è la probabilità **blendata col mercato**. Entrambe legittime, non la stessa misura — e il report precedente le ha confrontate come se lo fossero.

Caso reale (lo scarto **maggiore**, non la media): `Under 4` (OU), quota 1.5065. Se l'EV a ledger (+19.81%) venisse dalla probabilità registrata (0.6794), l'EV sarebbe +2.36%; invece corrisponde a una probabilità di **0.7953**. Lo stesso numero descrive una probabilità che sul ledger non c'è (scarto +17.46%).


### 2.2 Chi descrive i risultati: il mercato, non il modello

| grandezza | valore |
|---|---|
| EV del **modello** (quello a ledger) | +11.68% |
| EV della **probabilità blendata** | +4.90% |
| EV del **mercato** devigato | -1.16% |
| **realizzato** | -3.42% |
| distanza realizzato − mercato | -2.26% |
| distanza realizzato − modello | -15.10% |

Il realizzato sta **2.26% sotto il mercato** e **15.10% sotto il modello**: con un IC95 di -13.06%…+6.36% questo **non** dimostra che il mercato sia calibrato — dimostra che l'EV del modello è troppo alto di un ordine di grandezza rispetto al divario misurabile.

### 2.3 Scomposizione del divario (gli addendi sommano)

```
atteso (modello)   +11.68%
= definizione      +6.78%   (EV del modello vs prob. registrata)
+ edge dichiarato  +6.06%   (modello vs mercato devigato)
+ residuo          +2.26%   (non linearità: push, quote, blend)
realizzato         -3.42%
```

Divario totale **+15.10%**, di cui **+6.78%** di definizione (**45%** del divario) e +6.06% di edge dichiarato dal modello.

### 2.4 Per mercato

| mercato | n | EV modello | EV mercato | realizzato | IC95 realizzato | edge minimo |
|---|---|---|---|---|---|---|
| 1X2 | 71 | +6.41% | -0.81% | +7.39% | -9.95%…+24.88% | +27.73% |
| AH | 75 | +14.35% | -1.60% | -3.38% | -22.32%…+15.15% | +24.16% |
| OU | 189 | +13.95% | -1.10% | -6.60% | -21.33%…+7.67% | +15.04% |
| TENNIS | 41 | +5.45% | -1.23% | -7.53% | -34.28%…+20.39% | +40.05% |

**1X2 è l'unico mercato in cui l'EV descrive il risultato** (+6.41% atteso, +7.39% realizzato, n=71). **AH** è il mercato con lo scarto di definizione più alto (+10.02%) e il risultato peggiore (-3.38%): è la firma di una probabilità di modello sovrastimata **proprio dove il gate seleziona**.

### 2.5 Push, scadenze e popolazione

| popolazione | n | ROI flat | IC95 | EV a ledger |
|---|---|---|---|---|
| tutte le chiuse (incluse scadenze) | 455 | -2.83% | -10.72%…+5.14% | +11.49% |
| puntate effettive (scadenze escluse) | 399 | -3.22% | -12.46%…+5.71% | +11.80% |
| solo decise (won/lost) | 376 | -3.42% | -13.06%…+6.36% | +11.68% |

- **56 scadenze** (12.3% delle chiusure) sono righe mai giocate, chiuse come `push` perché il referto non ha trovato il risultato: pesano **+1.14%** sul divario.
- push **di mercato**: rimborso vero (lo stake torna indietro), non un errore: 23 fra le giocabili effettive.
- verdetti contraddittori: 0 · duplicati: 0.

### 2.6 Quanto è misurabile

Con n=455, deviazione standard 0.778 per unità e IC95 al 95%, l'edge minimo distinguibile è **+11.24%**. Un edge reale del 2% resta invisibile: **il campione non permette di confermare un vantaggio piccolo, solo di smentire un disastro**.

## 3. Il CLV positivo: abilità o margine?

### 3.0 Metodologia (verificata sul codice, non assunta)

- **formula**: `CLV = quota_segnale / quota_chiusura - 1` (`market_calib.clv_raw`). **Segno**: positivo = abbiamo preso un prezzo migliore di quello di chiusura. Un CLV medio positivo con P/L negativo è possibile e va spiegato, non nascosto.
- **precedenza della chiusura**: `closing_odds` (Pinnacle scritta dal job a T-0) → `closing_quota` → `pinnacle_quota`. La fonte viaggia con il campione (`closing_source`) e le coorti non si mescolano.
- **campione**: 475 osservazioni, di cui **325 echi** (chiusura identica al segnale: CLV 0 per costruzione) esclusi dalla coorte utile, che resta **150** righe.
- **fonti dichiarate**: `none` 423, `pinnacle_t0` 43, `sx` 9 (fonte non dichiarata: 423).
- **timestamps**: il CLV usa `closing_*` all'istante dell'ultima lettura (`updated_at`); la stabilità è misurata per settimana, perché una coorte concentrata in una sola settimana non distingue edge e periodo.

| campione | n | CLV medio | IC95 | positivi | realizzato (stesse righe) |
|---|---|---|---|---|---|
| tutte le chiusure | 475 | +0.55% | +0.27%…+0.82% | 20.0% | — |
| non-eco | 150 | +1.73% | +0.92%…+2.52% | 63.3% | -3.53% (n=72) |
| closing Pinnacle T-0 | 43 | +3.51% | +2.02%…+4.97% | 79.1% | -4.70% (n=30) |
| chiusura SX (stessa venue) | 9 | -0.53% | -0.95%…-0.13% | 22.2% | -61.02% (n=8) |

**325 osservazioni su 475** (68.4%) sono **echi**: la chiusura coincide con la quota di segnale, quindi il loro CLV è zero per costruzione. Includerli diluisce la media: il numero da leggere è quello dei non-eco.

### 3.1 Il controllo che decide: de-vig e stessa venue

| misura | n | valore | IC95 | verdetto |
|---|---|---|---|---|
| CLV vs quota sharp (con margine) | 34 | +2.70% | +0.52%…+4.81% | positivo |
| CLV vs **fair** (de-vigato) | 34 | -1.35% | -3.50%…+0.75% | NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG |
| margine medio misurato | 34 | +4.11% | — | 1.50× il CLV positivo |

Il margine **non è stimato**: `sum(1/quota)` è calcolato sulle rilevazioni sharp complete della **stessa partita** (tutti e tre gli esiti allo stesso istante). Il CLV contro la quota grezza contiene quel margine per costruzione: +4.11% di margine contro +2.70% di CLV.

Due conferme indipendenti che il numero è margine:

1. **de-vig**: togliendo il margine il CLV scende a -1.35% (IC95 -3.50%…+0.75%) e la quota di campioni positivi passa da 64.7% a 26.5%;
2. **stessa venue**: quando segnale e chiusura sono entrambi SX (nessun margine da attraversare) il CLV è -0.53% (n=9): **negativo**, cioè entriamo a un prezzo leggermente peggiore di quello di chiusura sullo stesso book.

### 3.2 Le due coorti nominate (+1,70% e +3,34%)

**+1.73% — non-eco**

- numerosità **150**, CLV +1.73% (IC95 +0.92%…+2.52%), 63.3% positivi;
- stabilità: **4 settimane** nel campione (2026-W39: n=3 -0.64%, 2026-W40: n=48 +2.89%, 2026-W41: n=75 +1.26%, 2026-W42: n=24 +1.18%);
- performance realizzata delle stesse righe: **-3.53%** su 72 chiuse (hit 45.8%), 78 righe non abbinabili a una previsione giocabile;
- selezione: è un sottoinsieme **non casuale** (le righe con chiusura catturata sono quelle con cache sharp disponibile) e questo è il limite principale della coorte;

**+3.51% — closing Pinnacle T-0**

- numerosità **43**, CLV +3.51% (IC95 +2.02%…+4.97%), 79.1% positivi;
- stabilità: **2 settimane** nel campione (2026-W41: n=40 +3.34%, 2026-W42: n=3 +5.80%);
- performance realizzata delle stesse righe: **-4.70%** su 30 chiuse (hit 53.3%), 13 righe non abbinabili a una previsione giocabile;
- selezione: è un sottoinsieme **non casuale** (le righe con chiusura catturata sono quelle con cache sharp disponibile) e questo è il limite principale della coorte;

Conclusione della sezione: **un CLV positivo non implica profitto**. Le due coorti hanno CLV positivo e realizzato negativo, e la spiegazione coerente con entrambi i numeri è il margine del book, non un'abilità di previsione.

## 4. Test pre-registrato

**Protocollo** `a4bf0184f77a9589` · candidati dichiarati **24** · split 50%/25%/25% cronologico (il test è l'ultimo tratto).

| tratto | righe | finestra |
|---|---|---|
| train | 1063 | — |
| validation | 532 | — |
| test | 532 | 2026-10-12 15:49:31.791710+00:00 → 2026-10-19 22:43:54.910858+00:00 |

**Candidato selezionato su validation** (regola: massimo ROI con n ≥ 40): `1X2<= 2.0 edge1% ev1.5% ALL` → -6.92% (n=100).

| insieme | n | ROI | IC95 | hit | EV a ledger |
|---|---|---|---|---|---|
| candidato (test) | 179 | -4.76% | -18.39%…+8.85% | 58.1% | +10.02% |
| benchmark: strategia corrente (test) | 60 | +6.41% | -13.66%…+25.69% | 65.0% | +6.07% |
| benchmark di MERCATO — descrittivo (test) | 0 | n/d | n/d…n/d | n/d | n/d |

**Esito: INCONCLUSIVE** — IC95 contiene lo zero. Batte il benchmark: **no**. Campione confidenziale (≥200): **no**.

Il **benchmark di mercato** (0 righe, ROI n/d) compra solo dove il prezzo offerto batte il prezzo equo devigato, senza usare il modello. È **descrittivo**: non entra nel verdetto (le regole di `pass`/`fail` sono quelle firmate prima del test). Serve a una domanda sola: il candidato batte anche chi non ha un modello?

Il benchmark ha **n=10** su validation: la strategia di produzione, con i suoi gate, seleziona così poche righe che **non è misurabile** (soglia dichiarata: 40). È un risultato in sé: il problema non è scegliere la soglia migliore, è che nessuna soglia produce abbastanza campione per dimostrare qualcosa.

## 5. Decisione

### nessun vantaggio dimostrato

- holdout INCONCLUSIVE: IC95 contiene lo zero

Regole applicate (dichiarate **prima** di eseguire il test, in `next_phase.DECISION_RULES`):

- **nessun_vantaggio**: il test pre-registrato sull'holdout NON passa (INCONCLUSIVE o FAIL) · oppure il CLV de-vigato non sopravvive (non distinguibile da zero) · oppure il candidato non batte il benchmark sulla stessa finestra
- **shadow**: il test PASSA (IC95 > 0) e batte il benchmark · ma il campione e' sotto le 200 chiusure oppure il CLV de-vigato non e' positivo: il risultato puo' essere rumore o selezione
- **candidato**: il test PASSA con n >= 200 · e il CLV de-vigato e' positivo con IC95 > 0 · e il candidato batte il benchmark sulla stessa finestra

### Cosa servirebbe per una prova controllata

```
min_closures      : 200
shadow_days       : 30
order_size_usdc   : 1.0
cb2               : ARMATO (soglia invariata: nessun ordine finche' l'equity non la supera)
live              : DISABILITATO
success           : CLV de-vigato > 0 con IC95 > 0 sul campione shadow
abort             : CLV de-vigato < 0 dopo 100 chiusure
```

**Non** tutte le condizioni sono soddisfatte oggi (test: INCONCLUSIVE, CLV de-vigato: NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG, n=179): il protocollo resta sulla carta finché una misura non lo giustifica.

## 6. Riproducibilità

```bash
venv/bin/python -m research_isolated.lab.next_phase --select-only  # nessun holdout
venv/bin/python -m research_isolated.lab.next_phase              # una volta
venv/bin/python -m research_isolated.lab.prereg --select-only
pytest research_isolated/tests -q
```

File prodotti: `NEXT_PHASE_REPORT.md`, `NEXT_PHASE_RESULTS.json`, `next_phase_markets.csv`, `next_phase_clv.csv`, `next_phase_candidates.csv`, `next_phase_log.txt`, `prereg_protocol.json`, `holdout_used.json`.

Lo snapshot è letto in `mode=ro`; nessun modulo di questo percorso importa `auto_bet`, `bot`, `tracker` o `execution_engine`.
