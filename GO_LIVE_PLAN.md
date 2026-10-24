# GO_LIVE_PLAN.md — come andare live e iniziare a monetizzare (24/10/2026)

> Documento operativo. Le cifre sono **misure** prese in sola lettura sul container
> AMS (`railway ssh --service betting_bot`, SQLite `mode=ro`, API pubbliche) il
> 24/10/2026, non stime. Le fonti esterne sono citate dove usate.
> Lo stato di memoria del progetto resta `AGENTS.md`; qui c'è **solo** il piano.

---

## 0. Sintesi

1. **Il live è riarmato** (`auto_bet_mode.json` → `live`) con l'inviluppo che segue.
2. **L'unico segnale statisticamente significativo del progetto è il CLV**:
   `+3,71%` medio su 63 campioni, IC95 `[+2,43%, +4,99%]`, p ≈ 0, 49/63 positivi
   (`clv_tracker.py --days 40`: `status = beating`, `reliable = true`).
3. **Le perdite recenti non sono "il modello che non funziona"**: sono tre
   sorgenti misurate, tutte chiuse oggi (§2).
4. **Il muro per monetizzare è il CAPITALE, non l'edge** (§3): con 19,40 USDC e
   1 USDC per ordine il sistema può produrre ~3 €/mese *anche se* l'edge regge.
5. **Destinazione consigliata**: Betfair.it (ADM, API ufficiale, tasse alla
   fonte) quando il capitale lo consente; SX resta il motore d'edge (§4).

---

## 1. Le misure che decidono

**Capitale**: equity `19,3978` USDC (17,3978 liberi + 2,00 in escrow), 2 bet aperte.
**Era live**: 89 puntate (3 aperte), 86 chiuse, 214,98 USDC puntati, **P/L −17,07** (ROI −7,9%).
**Ultimi 7 giorni**: 18 chiuse, **−19,79**.

**P/L per fascia quota (86 chiuse)**

| fascia | n | P/L | ROI |
|---|---|---|---|
| < 1,50 | 14 | **+8,71** | **+18,2%** |
| 1,50–1,80 | 26 | **−10,62** | −14,9% |
| 1,80–2,10 | 23 | **+4,11** | +7,3% |
| 2,10–2,50 | 13 | **−8,25** | −36,6% |
| ≥ 2,50 | 10 | **−11,02** | **−67,0%** |

**Previsioni giocabili chiuse, per mercato**

| mercato | n | P/L |
|---|---|---|
| 1X2 | 87 | **+4,47** |
| AH | 81 | −2,54 |
| TENNIS | 81 | −4,09 |
| OU | 220 | **−12,48** |

**CLV per sport e per fonte di chiusura**

| gruppo | n | CLV medio | quota positivi |
|---|---|---|---|
| **totale** | **63** | **+3,71%** | 77,8% |
| tennis | 32 | **+4,51%** | 93,8% |
| football | 25 | **+3,76%** | 76,0% |
| **football USA** | **6** | **−0,79%** | **0,0%** |
| chiusura `pinnacle_t0` | 51 | +4,49% | 90,2% |
| chiusura `sx` | 11 | −0,61% | 18,2% |
| **benchmark `betfair_t0`** | **10** | **−0,48%** | 50,0% |

> **Come va letto**: il CLV è positivo contro **Pinnacle** (lo sharp di
> riferimento) ma il secondo benchmark ha **10 campioni** e dice ≈ 0. Non è
> una smentita (troppo piccolo), è il motivo per cui la validazione continua.
> L'eco `sx` (chiusura presa sullo stesso exchange) non è un benchmark: −0,61%
> è la definizione di "nessun edge misurato su sé stessi".

**Crediti the-odds-api**: 412 residui, reset 01/11 (7 giorni), ritmo misurato
**76,2/giorno** contro **58,9/giorno sostenibili** → esaurimento previsto
**29/10, due giorni PRIMA del reset**. Era il vincolo operativo più urgente.

---

## 2. I quattro tagli applicati oggi (ognuno con la sua misura)

| leva | prima | ora | perché |
|---|---|---|---|
| `GRIDIRON_LANE` | 1 | **0** | football USA: CLV **−0,79%** con **0/6** campioni positivi e P/L **−56,4%**; il torneo che lo giustificava è chiuso il 20/10 |
| `TENNIS_ODDS_MAX` | 2,30 | **2,00** | le fasce 2,10–2,50 (−36,6%) e ≥2,50 (−67,0%) sono **tutte** fuori banda; il tennis resta la corsia con il CLV migliore (+4,51%) dentro 1,30–2,00 |
| `ORACLE_ENABLED` | 1 | **0** | oracolo a **linea** (OU/AH): 3 crediti/chiamata, **0 ordini in tutta la sua storia** (griglia SX a passi di 0,5 vs linea main di Pinnacle) |
| `SETTLEMENT_HEAL_INTERVAL_HOURS` | 12 | **48** | la verifica periodica ogni 12h era la voce di costo dominante del settlement |
| `ORACLE_H2H_BUDGET_DAY` | 40 | **16** | resta il percorso che sblocca i pick 1X2 (l'unico mercato con P/L positivo) |
| `ODDS_DAILY_BUDGET` | 60 | **30** | cap di rotazione: 30 leghe/giorno bastano (le leghe ammesse sono a 2 giorni) |
| `TENNIS_ORACLE_TTL_MIN` | 360 | **720** | metà dei refresh tennis, senza arrivare al tetto di 24h che scarterebbe pick in finestra |

Profilo atteso dopo il taglio: **~35-45 crediti/giorno** (da 76,2) → i 412
residui coprono i 7 giorni fino al reset con margine.

---

## 3. L'economia del monetizzare (la parte scomoda)

    profitto/mese ≈ stake × puntate/giorno × 30 × edge

con `edge = +3,7%` (il CLV misurato; il CLV è la migliore stima disponibile
del ROI atteso: comprare a un prezzo migliore della chiusura *è* l'edge).

| capitale | stake (1% o tetto) | puntate/giorno | **lordo/mese** | netto dopo 1% fee + tasse |
|---|---|---|---|---|
| **19,40 (oggi)** | 1,00 (tetto duro) | 2-3 | **~3 €** | ~2 € |
| 100 | 1,00 | 5 | ~5,5 € | ~4 € |
| 300 | 3,00 | 15-20 | ~55-67 € | ~40-50 € |
| 600 | 6,00 | 20 | ~133 € | ~95-105 € |
| 1.500 | 15,00 | 20 | ~333 € | ~240-260 € |

**Conclusione onesta**: a 19,40 USDC il sistema **non può** monetizzare: non è
una questione di soglie (tutte già allentate il 14/10), è che il prodotto
`capitale × puntate` è troppo piccolo. Il valore di questa fase non è il P/L:
è **finire la validazione** (CLV + settlement + esecuzione) spendendo il meno
possibile.

**Costi da mettere nel conto**

* **SX Bet**: **1% sull'utile netto di mercato, solo sui taker**; **0% sui
  maker** ([docs.sx.bet/user-guides/trading/fees](https://docs.sx.bet/user-guides/trading/fees)).
  Minimo ordine **1 USDC**; fondi in USDC on-chain (SX Rollup).
* **Betfair.it**: commissione sull'utile netto di mercato (tetto regolamentare
  10%), **imposta alla fonte**, min back **2,00 €** con incrementi di 0,50 €,
  vincita massima **10.000 €** per operazione (compresa la puntata).
  [Regole ADM per l'exchange italiano](https://support.developer.betfair.com/hc/en-us/articles/26096487231644-How-Do-I-Access-Betfair-Italy-Exchange-via-the-API)
* **Fisco**: con un operatore **ADM** non c'è nulla da dichiarare. SX **non è
  ADM**: le vincite vanno dichiarate (redditi diversi) — da inquadrare con un
  commercialista prima di alzare i volumi, perché **cambia il netto**, non il
  lordo. L'edge del 3,7% sopravvive a un'imposta del 20-26% *sui soli utili*,
  non a un prelievo sul transato.

---

## 4. Le cinque opzioni valutate

### A. SX Bet live — **scelta per ora** (unico venue che regge 19,40)
Edge migliore (prezzi da exchange piccolo, retail flow), 1 USDC minimo, 1% fee
taker. Contro: crypto (USDC su Arbitrum Orbit), fisco da gestire, profondità
limitata sui mercati sottili (fill rate storico 5,5% per tentativo, ~75% per
pick su 1X2 — il bot ritenta ogni 60s).
**Verdetto**: è il posto giusto per **misurare** l'edge, non per monetizzare a
questo capitale.

### B. Betfair.it (ADM) — **destinazione consigliata**
Legale, tasse alla fonte, liquidità di un exchange vero, e **API ufficiale
completa** (login certificato `identitysso-cert.betfair.it`, App Key,
`SportsAPING/v1.0/placeOrders`). Min back 2,00 € → capitale minimo sensato
**200 €** (2 € = 1%), operativo da **300-500 €**.
Nel repo **esiste già** `execution_engine.BetfairJsonRpcProvider` (stesso
protocollo, usato per gli aggregatori): un provider `betfair_it` è una
sottoclasse con login certificato + regole italiane, non un modulo nuovo.
**Contro**: prezzi più sharp di SX → l'edge è più sottile; serve l'account.

### C. Market-maker rewards su SX — **moltiplicatore, non alternativa**
SX paga pool USDC per ordini **limit** che battono la "global line" (Pinnacle),
restano sul book **≥ 10 s** e hanno stake minimo **~100 $** per ordine
([docs.sx.bet](https://docs.sx.bet/user-guides/rewards/maker-rewards)). Maker =
**0% fee**. Il codice c'è già: `resting_orders` (GTC) + `place_limit_order`.
Diventa possibile a **300-500 USDC**: si guadagna *anche senza vincere*, e
l'ordine limit "migliore della linea sharp" è esattamente la nostra tesi di
valore. **È la leva con il miglior rapporto rischio/rendimento del progetto.**

### D. Broker Betfair-compatible (BetInAsia BLACK / MollyBet)
Protocollo già implementato, ma **non ADM** (stesso problema fiscale di SX) e
stake minimi alti: nessun vantaggio a questo capitale.

### E. Solo paper
Costo zero, ma non produce il dato che manca: **il CLV si misura sui
riempimenti reali**, non sui segnali.

---

## 5. Configurazione live attiva (24/10/2026, 00:40 UTC)

| env | valore | effetto |
|---|---|---|
| `AUTO_BET_MODE` / kill switch | `live` | corsia reale armata |
| `ORDER_MAX_STAKE_USDC` | 1,00 | **tetto per ordine** (5,2% dell'equity) |
| `KELLY_MAX_STAKE_PCT` | 0,12 | cap dinamico 12% (non vincolante a 19,40) |
| `KELLY_MIN_TICKET_USDC` | 1,00 | sotto il ticket non si ordina |
| `OPEN_EXPOSURE_CAP_PCT` | 0,30 | max ~5 ordini aperti insieme |
| `DAILY_STOP_LOSS_PCT` | 0,25 | stop a −4,85 USDC |
| `WEEKLY_STOP_LOSS_PCT` | 0,40 | stop a −7,76 USDC |
| `T60_KILL_WALLET_USDC` | **12** | CB2: arresto con 62% del capitale ancora in cassa |
| `T60_WINDOW_MIN_MIN` / `MAX_MIN` | 720 / 2 | finestra esecutiva T-720..T-2 |
| `GRIDIRON_LANE` | **0** | corsia football USA spenta |
| `TENNIS_ODDS_MAX` | **2,00** | tennis solo 1,30-2,00 |
| `ORACLE_ENABLED` / `ORACLE_H2H_BUDGET_DAY` | **0** / 16 | oracolo a linea spento, h2h attivo |
| `SETTLEMENT_HEAL_INTERVAL_HOURS` | 48 | meno crediti spesi in verifica |
| `ADAPTIVE_WEIGHTING` | 1 | stake per lega dal CLV rolling (solo riduzione) |

Revert immediato: `/autobet off` (Telegram) oppure
`data/execution/auto_bet_mode.json` = `{"mode":"off"}`.
Ogni altra soglia è una env dichiarata in `preserve()` di `.railway/railway.ts`.

---

## 6. Gate congelati per alzare lo stake (nessuna eccezione)

Lo stake si alza **solo** se tutte e tre valgono:

1. **CLV**: n ≥ 50 campioni con IC95 **tutto sopra 0** e conferma del secondo
   sharp (`betfair_t0`, oggi n=10: servono ≥ 20 chiusure);
2. **ROI**: ≥ 30 chiusure dell'era con IC95 del ROI sopra 0
   (`significance.py`), non un ROI puntuale;
3. **Capitale**: stake 1% ⇒ equity ≥ 100 (per 1,00 €); per i maker rewards SX
   servono **≥ 300-500** (ordini da 100 $).

Corollario misurato: a quota media 1,65 servono **~12.700 chiusure** per
confermare un edge del +2% — quindi il gate 1 (CLV) è l'unico realistico, ed è
il motivo per cui si misura il CLV e non il P/L.

---

## 7. Cosa serve dal proprietario

1. **Capitale** — una delle tre: restare a 19,40 (validazione, ~0 €/mese),
   top-up **100-300 €** (operativo), top-up **500 €+** (maker rewards + crescita).
2. **Betfair.it** — aprire/verificare l'account ADM e generare **App Key +
   certificato** (login non interattivo). Senza questi il venue legale resta
   teorico; con questi il provider si scrive (mezza giornata, codice già a metà).
3. **Commercialista** — inquadrare le vincite SX (non ADM) *prima* di alzare i
   volumi: cambia il netto, non il lordo.

---

## 8. Incidente del volume PIENO (24/10, 00:39-00:41 UTC) — risolto

**Non era un problema di configurazione del bot: era il disco al 100%.**
`df -h /app/data` → `434M usati su 434M, 0 disponibili`. Cause in ordine:

1. `BACKUP_KEEP=7` × snapshot da **~70-82 MB** (che copiano TUTTA `data/`) = fino a
   **490 MB** su un volume da 434 MB: la rotazione era strutturalmente
   insufficiente rispetto alla dimensione dello snapshot.
2. Il 23-24/10 si erano accumulati **5 snapshot per 354 MB** (di cui `data/`
   stesso pesa ~70 MB: DB 20, `execution` 29 di log, `decision` 15).
3. Il backup di BOOT del nuovo container (00:39) ha riempito gli ultimi MB →
   da quel momento **231 errori `ENOSPC`**: DB (`database or disk is full` in
   `sx_signals_job`), stima di Dixon-Coles, sink di osservabilità e comandi
   shadow. Nessuna puntata era in corso (`0 candidati`), quindi **nessun dato
   di denaro perso** — ma il bot non avrebbe potuto né saldare né ordinare.

**Rimedio applicato (verificato sul container)**

* rimossi i 4 snapshot più vecchi, tenuto il più recente completo → **140 MB
  usati, 284 liberi (33%)**;
* `BACKUP_KEEP=2` su Railway (già `preserve()` in `.railway/railway.ts`) →
  massimo 2 × ~70 MB = 140 MB + dati vivi ~70 MB = **210 MB su 434** (50%);
* backup manuale di verifica: `status ok`, 117 voci, `data_errors 0`,
  `PRAGMA integrity_check: ok`, DB con 2331 previsioni / 89 bet / 497 CLV /
  17938 risultati; rotazione telemetria attiva (`scanned 10`, 0 errori).
* scrittura su DB riprovata a mano: **ok**, `quick_check: ok`.
* prova di ripresa: subito dopo, `market_shadow_job` ha scritto **348 quote su
  107 fixture** (prima falliva).

**Regola operativa nuova**: `dimensione_snapshot × BACKUP_KEEP` deve stare sotto
**metà** del volume, altrimenti il backup di boot è un interruttore che spegne il
bot. Con questo volume: `BACKUP_KEEP <= 2`. Se si vuole più storia, si sposta il
backup FUORI dal volume (destinazione esterna) o si escludono le cache quote
dallo snapshot: **mai** alzare `keep` su un volume condiviso con il DB.

---

## 9. Ordine dei prossimi passi tecnici

1. **Provider Betfair.it** (`BetfairItProvider` ⊂ `BetfairJsonRpcProvider`):
   login certificato, min 2,00 € + incrementi 0,50, mappatura mercati/runner,
   settlement via `listClearedOrders`; test offline con trasporto iniettato.
   *Prerequisito: §7.2.*
2. **Maker lane SX** a capitale ≥ 300: `resting_orders` con stake ≥ 100 $ sulle
   mainline eleggibili → rewards + 0% fee. *Prerequisito: §7.1.*
3. Rimisurare il flusso a 7 giorni (`flow_measure.py --hours 168`) per verificare
   che i tagli di oggi non abbiano azzerato i candidati giocabili.
4. Riaprire i budget crediti (rotazione/h2h) solo dopo il reset del 01/11.
