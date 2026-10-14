# QUANT_MODULES.md — 6 moduli quantitativi avanzati: fattibilità, architettura, roadmap

> Documento di riferimento per il programma quantitativo avviato il 14/10/2026.
> Le regole di progetto che governano OGNI riga di questo piano stanno in
> `AGENTS.md`: la strategia e' l'elemento piu' importante, ogni formula vive in
> UN solo posto, il denaro si tocca solo dopo una misura, e nessuna soglia si
> cambia senza un tripwire che la fissi.

---

## 0. Regola di promozione (vale per tutti e sei i moduli)

Nessun modulo entra nel percorso del denaro al primo rilascio. La promozione e'
a **tre stadi**, e ogni stadio ha una porta misurabile:

| stadio | cosa fa | cosa NON fa | porta di uscita |
|---|---|---|---|
| **SHADOW** | calcola, logga, scrive telemetria | non tocca EV, stake, gate | il campione raggiunge la soglia dichiarata (`significance.MIN_SAMPLES` = 30 chiusure, o la soglia propria del modulo) |
| **GATE** | puo' RIFIUTARE (o solo restringere) un candidato | non puo' promuovere un candidato che i gate esistenti hanno gia' bocciato | il rifiuto e' misurabile come costo/beneficio (`league_gate_impact`/`decision/compare` come modello) |
| **SIZING** | puo' solo RIDURRE lo stake (mai alzarlo) | non puo' scavalcare cap, recinto, kill-switch, stop | parita' con `auto_bet` su una griglia di casi |

Il verso di marcia e' **sempre lo stesso**: un modulo nuovo puo' **togliere**
(skepsi, restrizioni, stake), mai **aggiungere** rischio. Un modulo che alza lo
stake e' una decisione di strategia del proprietario, non un effetto collaterale
di un'integrazione.

---

## 1. Audit dello stato attuale (cosa esiste GIA', misurato il 14/10/2026)

Prima di scrivere una riga: 4 dei 6 moduli **esistono in tutto o in parte** nel
repo. Il lavoro reale non e' "integrarli", e' chiudere i gap dichiarati qui sotto.

| # | modulo | cosa esiste | gap reale | dipendenza esterna |
|---|---|---|---|---|
| 1 | ML avanzata (GBM + feature) | `ml_ensemble.py` (XGBoost **presente**, 3.4.1), `probability_calibration.py` (isotonica PAVA), `ml_dataset.py`, `ml_audit.py`, `drift_monitor.py`, retrain 05:45+boot, `backtest_mc.py` walk-forward, **`match_features.py` (feature builder, Fase 1 fatta il 14/10)** | **campione** (ledger ~54 righe chiuse) e **misura di Brier walk-forward** con le feature nuove | `soccerdata`->FBref (xG) gia' installato; `match_results` 17k righe per riposo/densita' (gratis) |
| 2 | Line movement / sharp sentiment | `line_movement.py` (snapshot in `price_snapshots`), `rlm_alert.py` (RLM/steam/crollo, job 5'), `steam_move.py` (ΔQ/Δt su Pinnacle, priorita' d'esecuzione), `bookmaker_advantage.py`, `market_signals.py`, `book_flow.py` | **niente da costruire**: il modulo e' completo. Manca solo la resa **misurabile** (CLV dei pick marcati steam vs non marcati) | nessuna |
| 3 | Inferenza bayesiana | `rating_engine.py`: coefficienti attack/defence con **time-decay** (half-life 100gg) e **shrinkage verso il prior** su `n` REALE (prior 6 partite) — e' un empirico-bayesiano | **incertezza** (posterior std) e struttura gerarchica lega->squadra | `match_results` 17k righe (gratis) |
| 4 | Lineup window execution | la **finestra** (T-180..T-2), `t60_dispatch_pending`, `resting_orders` (GTC), `closing_line.py` (T-0), `live_intel` news (titoli formazioni/infortuni) | **feed strutturato delle formazioni ufficiali**: oggi esistono solo titoli di news non gate-abili | API-Football `/fixtures/lineups` (piano free esclude la stagione corrente, account sospeso a settembre) o scraping FBref (fragile) |
| 5 | Catene di Markov (tennis) | `tennis_quant.py` (ELO superficie + Poisson hold/break, gate "entrambi confermano", telemetria), `tennis_sandbox.py` (ELO paper), `tennis_lane.py` (ordini live via oracolo Pinnacle 2 vie) | **dati di servizio** (spw/rpw per superficie): senza, un simulatore point-by-point e' solo una riparametrizzazione del ponte ELO→probabilita' | nessuna fonte di statistiche di servizio nel repo |
| 6 | Portfolio theory & copula | **implementato il 14/10** (`portfolio_risk.py` misura + **`copula_portfolio.py` GATE in ENFORCE** dal 14/10/2026) | Fase 2 restante: `allocate()` (allocazione ottima con vincolo di coda, oggi c'e' il solo gate) | nessuna |

---

## 2. Architettura: dove si innesta ogni modulo

La pipeline ha **quattro punti di innesto** e ogni modulo appartiene a UNO solo.
E' la mappa che impedisce a due moduli di governare la stessa decisione.

```
                     ┌──────────────────────────── FONTI ───────────────────────────┐
                     │  SX Bet (book) · Pinnacle (oracolo sharp, Shin) · live_intel   │
                     │  (xG/ELO/news) · match_results (17k) · ledger (predictions/bets)│
                     └───────────────────────────┬───────────────────────────────────┘
                                                 │
   ┌─────────────────────────────── 1. FEATURES / PROBABILITA' ───────────────────────────┐
   │  poisson_engine (Dixon-Coles) ──► rating_engine (time-decay+shrink)                  │
   │  [M1] feature builder (xG, riposo, densita') ──► [M1] GBM/LR ──► isotonica          │
   │  [M3] bayes_ratings (posterior mean + std)   ──► incertezza → gate qualita' dati     │
   │  [M5] Markov tennis (spw/rpw → distribuzione punteggi)                               │
   └──────────────────────────────────────────┬───────────────────────────────────────────┘
                                              │  p_model, p_true, λ, incertezza
   ┌──────────────────────────────────────────▼──── 2. VALIDAZIONE / GATE ────────────────┐
   │  value_filter.is_sane · gate di lega · favourite gate · fascia quota                 │
   │  oracolo Pinnacle (h2h/linea) · [M2] steam/RLM come PRIORITA' (non gate)             │
   │  decision/risk_engine (kill-switch > stop > feed > qualita' dati > cap)               │
   └──────────────────────────────────────────┬───────────────────────────────────────────┘
                                              │  candidati approvati
   ┌──────────────────────────────────────────▼──── 3. SIZING / PORTAFOGLIO ──────────────┐
   │  stake_engine.dynamic_kelly_fraction (k 0.15-0.25) · cap 12% · ticket 1.00           │
   │  ** [M6] portfolio_risk: distribuzione ESATTA del P/L del blocco correlato **         │
   │  auto_bet: correlation cap 30% · recinto esposizione aperta 50% · CB2 · stop          │
   └──────────────────────────────────────────┬───────────────────────────────────────────┘
                                              │  stake per singolo ordine
   ┌──────────────────────────────────────────▼──── 4. ESECUZIONE / TIMING ───────────────┐
   │  finestra T-180..T-2 · IOC con floor EV · resting GTC · ricircolo (rush)              │
   │  [M4] lineup window: attesa della formazione ufficiale + adeguamento mercato         │
   └──────────────────────────────────────────────────────────────────────────────────────┘
```

**Perche' M2 non e' un gate.** RLM/steam sono **reazione al mercato**: usarli
come gate significherebbe far decidere al movimento lo stesso prezzo che stiamo
gia' giudicando con l'oracolo sharp — due giudici sulla stessa domanda, e il
secondo (l'oracolo) e' piu' diretto. Restano dove sono: **priorita'
d'esecuzione** (chi ha lo steam davanti entra prima, perche' il prezzo si sta
chiudendo). L'unico modo di promuoverli a gate e' misurare prima che il gruppo
"steam" abbia CLV **misurabilmente** migliore (Fase M2-2 qui sotto).

**Perche' M6 non usa una copula.** Per gambe sulla **stessa partita** la
dipendenza non e' un parametro da stimare: e' una **funzione esatta del
punteggio**, e il progetto modella gia' quella distribuzione (griglia
Poisson+Dixon-Coles). Fittare una copula su un oggetto la cui legge congiunta
e' nota **aggiunge errore**. Per gambe su partite **diverse** non esiste una
variabile condivisa: la copula andrebbe stimata dalla storia, ed e' esattamente
cio' che fa `empirical_block_correlation` (con proxy e baseline dichiarati).
La copula e' quindi lo strumento di **seconda scelta**, da aprire solo se la
correlazione empirica misurata risulta **materiale e stabile** — decisione
misurata, non architetturale.

---

## 3. Fase 1 — FATTA: `portfolio_risk.py` (modulo 6, solo misura)

**File**: `portfolio_risk.py` (815 righe) + `test_portfolio_risk.py` (61 test, offline).

Cosa calcola, e perche' e' diverso da una stima:

- **`block_distribution(legs, lam_h, lam_a)`** — distribuzione **ESATTA** del
  P/L di un blocco di esiti sulla stessa partita: ogni cella della griglia
  punteggi assegna un P/L determinato a **tutte** le gambe insieme. Quindi
  media, dev.std., p5/p50/p95, `P(perdita)`, `P(perdita oltre 5%/10%/20% del
  bankroll)`, `max_loss`/`max_win` e **lo scenario peggiore** (quale punteggio
  fa male) sono proprieta' della distribuzione: **nessun rho da indovinare**.
- **`correlation_matrix`** — correlazione esatta fra le gambe (dai punteggi
  condivisi: il pareggio e' positivamente correlato con l'Under, il favorito
  casa con l'Over, i due lati di un AH con -1).
- **`diversification`** — std congiunta vs std se le gambe fossero indipendenti:
  **quanto il cap del 30% sta proteggendo davvero** (e' la misura che dice se
  la costante scelta a mano ha un fondamento).
- **`empirical_block_correlation`** — per partite diverse: proxy **dichiarato**
  (indicatore binario casa/trasferta, coppie nella stessa lega+giornata), con
  **baseline fra blocchi diversi** (se anche la baseline fosse alta, il proxy
  starebbe leggendo un trend comune e non una correlazione) e campione minimo
  (sotto 200 coppie: `insufficient`, rumore).
- **`production_caps`** — i cap di produzione (30% correlazione, 50% recinto)
  **letti da `auto_bet`**, mai copiati: il report confronta sempre il cap vero
  con il rischio misurato.

Confini (tripwire nei test): sola lettura (`mode=ro`; il test tenta una `UPDATE`
e pretende che fallisca), zero rete, zero crediti, **zero ordini**, nessuna
scrittura sul ledger, nessuna soglia di rischio scritta a mano nel sorgente,
`import portfolio_risk` non carica `auto_bet`/`bot`/`tracker`.

**Due cose scoperte implementandolo** (entrambe corrette, entrambe difese da un
test):

1. **Il verso della linea AH.** `multi_market.order_target` restituisce la linea
   dal punto di vista di **teamOne** (casa) — e' la convenzione della
   risoluzione del mercato su SX. `poisson_engine.ah_outcome_probs` vuole invece
   la linea **del lato scelto**: per l'ospite il segno va ribaltato. Senza quel
   passaggio `Home -0.75` e `Away +0.75` (lo stesso mercato visto dai due lati)
   **non sommano a 1**, e la correlazione fra le due gambe non e' -1.
2. **Un test sulle date/conteggi puo' cadere a caso.** La baseline empirica su
   6 giornate si stima su ~15 coppie e vale -0.5 (rumore); su 20 giornate vale
   -0.10 (una stima). Un'asserzione su un campione da rumore e' un test che cade
   da solo — la stessa lezione delle date fisse del 15/09.

**Uso**: `venv/bin/python portfolio_risk.py [--json] [--days N] [--bankroll N]`
e la misura ad hoc
`venv/bin/python portfolio_risk.py --block "1X2:1:1.65:5" --block "OU:Over 2.5:1.90:5" --lambdas 1.6 1.1 --bankroll 47`.

---

## 3-bis. Fase 1 — FATTA: `match_features.py` (modulo 1, feature engineering)

**File**: `match_features.py` (28 feature) + `test_match_features.py` (**105 test, offline**).
Solo misura: non tocca `ml_ensemble.FEATURE_NAMES`, non cambia nessuna
probabilita' di produzione, nessun gate, nessuno stake.

Cosa produce, da dati che abbiamo gia' e a costo zero:
- **riposo e densita' di calendario** per lato: `rest_days` (giorni dall'ultima
  partita), `matches_7d/14d/28d`, `congested` (>=3 partite in 7 giorni),
  `short_rest` (<=3 giorni di riposo);
- **forma recente** sulle ultime 5: `pts5`, `gf5`, `ga5` (gol letti dal punto di
  vista della squadra: in trasferta sono quelli del lato ospite);
- **contesto situazionale**: `is_weekend`, `kickoff_hour` (UTC), `month`;
- **xG/xGA** da `live_intel` (FBref): feature **best effort** che si attiva solo
  con `--xg`/`xg_fn` iniettato — spenta, la misura resta `None` e `xg_present` 0;
- **`hist_matches`** per lato: il segnale di COPERTURA che distingue "squadra
  nota che ha giocato 0 partite" da "squadra che il DB non conosce".

L'invariante centrale — **nessun look-ahead**: le feature di una partita al
kickoff `T` usano solo risultati con data **strettamente** `< T` (la partita
stessa e' esclusa). Verificato su due DB che differiscono per UN risultato
successivo: le feature della partita precedente sono **identiche**, e la
controprova (una partita dopo) cambia davvero. Un modello con feature che
vedono il futuro misura se stesso.

**L'xG e' escluso dalla scrematura storica** (`LOOKAHEAD_UNSAFE`): l'aggregato di
`live_intel` e' di STAGIONE, quindi include le partite successive al kickoff.
Sulla storia sarebbe look-ahead; sui match **correnti** e' invece la fotografia
della squadra oggi — due popolazioni, dichiarate.

**Scrematura**: per ogni feature l'**AUC di Mann-Whitney** (0.5 = nessuna
informazione) con correzione per i pareggi, p-value senza dipendenze nuove
(`math.erfc`, niente scipy), e `useful` solo con |AUC-0.5| >= 0.02 **e** p < 0.05.
Media dei ranghi scritta a mano (niente numpy dove basta Python).

**Esito REALE (container, 14/10/2026, 17.127 partite di storia):**
```
✅ pts5_home         AUC 0.5585 p=1.4e-39      ✅ ga5_away        AUC 0.5416 p=6.9e-21
✅ gf5_home          AUC 0.5583 p=1.8e-39      ✅ rest_days_home  AUC 0.4683 p=1.1e-12
✅ pts5_away         AUC 0.4447 p=1.1e-35      ✅ matches_14d_home AUC 0.5297 p=1.9e-12
✅ gf5_away          AUC 0.4515 p=7.6e-28      ✅ matches_7d_home AUC 0.5219 p=3.7e-08
✅ ga5_home          AUC 0.4566 p=1.4e-22      ✅ matches_28d_home AUC 0.5297 p=8.1e-12
scartate: 64 bad_ts, 352 righe senza storia per nessuna delle due squadre
copertura: rest_days 99%, pts5 99%, xG 0% (percorso a richiesta)
```
10 feature passano la soglia, **tutte nella direzione giusta** (la forma
`_away` e' correlata negativamente con la vittoria casalinga, come deve).
Magnitudine 0.44-0.56: piccola, come in letteratura — un effetto di 8 punti di
AUC su 17k partite non e' rumore, ma nemmeno una scorciatoia.

**Tre difetti trovati implementando e correggendolo** (tutti difesi da un test):
1. **`history_feature_rows` passava l'epoch (float) a `features_for`**, che si
   aspettava una data ISO: **tutte** le righe finivano con "squadra sconosciuta"
   (0 feature). Fix alla radice: `_epoch` accetta anche un epoch numerico, una
   sola strada per l'istante (un `bool` NON e' un epoch).
2. **`xg_present` restava `None`** quando l'xG non era richiesto: un flag deve
   essere un flag. Ora vale 0/1 sempre, e la MISURA (`xg_for_*`) resta `None`.
3. **`Campione: None partite`** nel report e `una_sola_classe` su un campione
   VUOTO: "dato assente" e "classi sbilanciate" sono diagnosi diverse e
   mandavano a cercare il problema nella direzione sbagliata. Nuovo motivo
   `nessun_dato`, e il campione dichiarato nel payload.

---

## 4. Roadmap per modulo (ordine consigliato, per costo/beneficio)

L'ordine non e' quello della richiesta: e' quello che **non tocca il denaro
prima di aver misurato**, e che non crea dipendenze a pagamento senza ritorno.

### M6-2 — Ottimizzazione del portafoglio (Fase 2 del modulo 6) — *prossimo, gratis*
**Cosa**: `allocate(legs, lam_h, lam_a, bankroll, cap)` che sceglie la
dimensione ottima dello stake vettoriale minimizzando `P(drawdown > soglia)`
(o massimizzando l'EV sotto un vincolo di coda) sulla **griglia esatta** gia'
disponibile in Fase 1.
**Perche' prima degli altri**: la matematica esiste, non serve nessuna fonte
nuova, e il risultato si confronta 1:1 con il cap del 30% oggi scelto a mano.
**Porta di uscita**: riduzione misurata di `P(perdita oltre 10%)` a parita' di
stake totale, su un campione di blocchi storici ricostruiti dal ledger.
**Cosa NON fa**: non alza il totale; puo' solo **ridistribuire** lo stake dentro
il tetto deciso dai cap di produzione.

### M1 — Feature engineering per il modello ML — *fatta la parte feature (14/10)*
**Stato**: `match_features.py` **implementato e misurato** sui 17k storici
(sezione 3-bis). Resta la **porta di uscita**: il Brier walk-forward con le
feature nuove contro le 15 attuali. Fino a quella misura la produzione non
cambia di una riga.
**Cosa**: `match_features.py` che costruisce feature **gratis e verificabili**
dai dati che abbiamo gia':
- **giorni di riposo** per squadra, **densita' di calendario** (partite negli
  ultimi 7/14 giorni) e **turni infrasettimanali** — calcolabili dai 17k
  `match_results` con un tripwire sui casi di confine (0 giorni di riposo,
  doppia partita nello stesso giorno);
- **xG/xGA** da `live_intel` (FBref via `soccerdata`, gia' installato e con
  cache su disco): **feature dichiarata "best effort"** — se il provider non
  risponde la feature e' **assente**, mai 0 (uno 0 vero e uno 0 inventato
  insegnano due cose diverse al modello);
- **contesto situazionale**: derby (stessa citta'), coppa vs campionato, ultima
  giornata di stagione.
**Porta di uscita**: Brier del modello con le feature nuove **migliore** di
quello con le sole 15 feature attuali, su **split walk-forward senza
look-ahead** (`backtest_mc.py` e' il precedente: il modello dell'epoca si
addestra solo sulle giornate precedenti).
**Cosa NON fare**: **non** sostituire `FEATURE_NAMES` in produzione prima di
quella misura; un cambio di feature cambia le probabilita' che il bot usa.
**Nota onesta sul campione**: il ledger ha ~54 chiusure; con n cosi' piccolo un
GBM sovra-adatta. Le feature si addestrano sulla **storia** (17k partite) e si
misurano sul ledger: sono due popolazioni diverse e il report deve dirlo.
**LightGBM**: `xgboost` e' gia' in `requirements.txt` e nel codice. Aggiungere
`lightgbm` e' una riga, ma il guadagno su ~1000 righe di training e' rumore:
**non aggiunto** (la leva e' la feature, non il booster).

### M3 — `bayes_ratings.py` (posterior + incertezza) — *fattibile ora, in shadow*
**Cosa**: stima Gerarchica Normale-Poisson di attacco/difesa per squadra con
prior per lega, aggiornata sui `match_results`, che produce **media E deviazione
standard** per squadra (numpy-only: MAP via IRLS o campionamento Gibbs leggero —
zero dipendenze nuove).
**Perche' ha valore**: oggi `decision/risk_engine.py` manda in `review` un
segnale quando la **copertura** dei rating e' bassa (conta le partite). Con
l'incertezza il gate diventa **quantitativo**: `review` quando l'intervallo di
confidenza su λ e' largo, non quando il conteggio e' piccolo.
**Vincolo dichiarato (`AGENTS.md`)**: il fix del 31/08 sul **shrink su `n`
reale** e' il motivo per cui il modello funziona. Il modulo bayesiano **non
sostituisce** `rating_engine`: affianca, e la promozione richiede un test di
**parita'** sui coefficienti (differenza media entro una soglia dichiarata) e
un confronto di Brier sulla storia.
**Porta di uscita**: Brier del bayesiano <= Brier di `rating_engine` sulla
storia, E l'incertezza misurata **correlata** con l'errore reale (se non lo e',
l'incertezza non dice nulla e il gate non guadagna niente).

### M2-2 — Rendere MISURABILE il valore di steam/RLM — *piccolo, gratis*
**Cosa**: segmentare il CLV (gia' misurato da `clv_tracker.py`) fra pick
**marcati steam** e non marcati, e fra pick con RLM a favore e contro.
**Perche'**: e' l'unico modo di trasformare un'euristica in un gate.
**Porta di uscita**: differenza di CLV fra i due gruppi > 0 sull'intervallo di
confidenza (non sul punto) e campione >= 30 per gruppo. Altrimenti il modulo
resta una priorita' d'esecuzione — che e' gia' utilissimo e non fa danno.

### M5 — Markov tennis — *da aprire solo con i dati di servizio*
**Cosa**: motore point→game→set→match (alternanza servizio, tie-break,
best-of-3/5) che prende in ingresso **spw/rpw per superficie** e produce la
distribuzione di punteggi (set, game totali).
**Il punto**: senza **spw/rpw misurati** il simulatore non aggiunge informazione
rispetto al Poisson hold/break gia' in `tennis_quant.py` (che ha un fallback
dichiarato ELO→hold). Con spw/rpw **sì**: il Markov genera anche i mercati
derivati (game totali, punteggio esatto in set) che oggi non copriamo.
**Porta di uscita**: Brier del Markov < Brier del Poisson hold/break sui match
tennis **saladati**; e il verdetto deve dichiarare `serve_stats_source` (mai un
fallback spacciato per una misura — la regola esiste gia' in `tennis_quant`).
**Blocco attuale**: nessuna fonte di statistiche di servizio nel repo. La misura
che serve prima e': *quanti* match tennis chiusi abbiamo per superficie
(se sono <30 per superficie, il modulo non e' distinguibile dal rumore).

### M4 — Lineup window execution — *rinviato, con motivo misurato*
**Cosa esisterebbe**: attendere il rilascio delle formazioni ufficiali
(~60-75' prima) e **rientrare nel mercato** nell'intervallo fra il rilascio e
l'adeguamento.
**Perche' rinviato**: la finestra esiste gia' (T-180..T-2, estesa il 12/10) e
**non e' il collo di bottiglia**. Il collo di bottiglia misurato e' il **numero
di candidati** (0-2/giorno con i gate attuali) e il **fill rate** (5,5% prima
del fix del troncamento). Un gate sulle formazioni affinerebbe un filtro che
oggi scarta quasi tutto.
**Cosa serve, in ordine**: (1) una fonte **strutturata** di formazioni ufficiali
(API-Football `/fixtures/lineups` — il piano free esclude la stagione corrente
e l'account e' stato sospeso a settembre; oppure scraping FBref, fragile);
(2) la misura di **quanti pick tocca** il gate (se toccano 2 pick/settimana,
il beneficio non paga il costo in crediti e fragilita').
**Nota**: `live_intel` ha gia' le **news** (titoli su infortuni/formazioni).
Non sono gate-abili (testo libero, non un dato strutturato) e restano un
**puntatore alla fonte** come da progetto.

---

## 3-ter. Fase 2 — FATTA: `copula_portfolio.py` in ENFORCE (14/10/2026)

**File**: `copula_portfolio.py` (1360 righe) + `test_copula_portfolio.py`
(**93 test, tutti offline**) — **collegato** al percorso ordini LIVE.

E' il gate che risponde alla domanda lasciata aperta dalla Fase 1: *cosa fare*
del rischio misurato. Direttiva del proprietario: **ENFORCE** (nessuna shadow),
moltiplicatore in **[0, 1]** (puo' solo ridurre o azzerare), correlazione bassa
-> 1.0, moderata -> riduzione proporzionale, **critica -> ordine BLOCCATO** con
lo scarto loggato.

Perche' dopo il tetto e non subito dopo il Kelly: i layer sopra al Kelly
(torneo x1.5, rush finale) **alzano** lo stake; la coda di portafoglio dipende
dagli importi, quindi il gate deve misurare lo stake **definitivo**. Dopo di lui
non c'e' nessun layer che possa rialzare cio' che ha ridotto.

Tre vincoli, vince il piu' severo: **dipendenza effettiva** (correlazione +
coda della t-copula: `same_match` 0.89 = critica), **cap di cluster** (50% del
bankroll per lega) e **budget di coda** (perdita al quantile 5% <= 35% del
bankroll). Con `lam_h`/`lam_a` da `match_analysis` la distribuzione del P/L e'
**esatta** (griglia Dixon-Coles di `portfolio_risk`); senza, di **mercato** e la
fonte e' dichiarata.

Due bug reali trovati misurando (entrambi corretti e difesi da test):
1. **fattore di coda condiviso da tutte le unita'** -> dipendenza inventata fra
   unita' dichiarate indipendenti (6 unita' a rho=0: P(0 perdenti) 0.277 invece
   di 0.041). Ora la miscelazione chi-quadro e' **per componente connessa**;
2. **cap di cluster misurato sulla somma delle puntate** invece che sul
   bankroll -> su portafoglio vuoto il budget era 0 e il primo pick veniva
   bloccato "per concentrazione di se' stesso" (il gate non lasciava passare
   nessun ordine).

**Il criterio non si assume monotono**: il budget di coda si **valuta** su ogni
punto di una griglia di 41 moltiplicatori e si sceglie il piu' alto feasible
(misurato: la stessa terna dava perdita di coda 16.5 con 5 unita' e 13.0 con 6:
su una quantile empirica discreta alzare lo stake puo' alzare il quantile).

Sempre in **fail-safe**: un errore di calcolo lascia i candidati **invariati**
(un guasto non e' un rischio misurato). Non gira in **SIM** (la cassa simulata
alimenta ML e CLV: cambiarne l'era sarebbe un effetto collaterale).

---

## 5. Env, flag e reversibilita'

Tutti i moduli nuovi seguono lo schema del progetto: **spenti di default,
accendibili da env, senza redeploy quando la lettura e' a RUNTIME** (le
eccezioni sono dichiarate nel modulo). Ogni env va in `preserve()` dentro
`.railway/railway.ts` **prima** di essere impostata, altrimenti un
`railway config plan` la segnala come distruzione e un futuro apply la cancella
(regola del 28/09).

Per `portfolio_risk.py` **non serve nessuna env**: e' una diagnostica, non ha
soglie da configurare, e i cap che mostra li legge dai moduli che li applicano.

---

## 6. Stato e prossimo passo

| modulo | stato | prossimo passo |
|---|---|---|
| 6 — Portfolio theory | **Fase 1 FATTA** (misura) + **Fase 2 FATTA** (`copula_portfolio` in ENFORCE nel percorso ordini LIVE) | `allocate()` (allocazione ottima con vincolo di coda) |
| 1 — ML/feature | **Fase 1 FATTA** (28 feature, 10 sopra soglia su 17k partite) | misura di Brier walk-forward **prima** di toccare `FEATURE_NAMES` |
| 3 — Bayesiano | empirico-bayesiano presente | `bayes_ratings.py` con incertezza, parita' + Brier |
| 2 — Line movement | **completo** | M2-2: segmentazione CLV steam/non-steam |
| 5 — Markov tennis | ELO+Poisson presenti | aprire il motore quando esiste una fonte spw/rpw |
| 4 — Lineup window | finestra presente | rinviato fino a volume di candidati |
