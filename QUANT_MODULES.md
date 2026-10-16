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

## 3-quater. Fase 1 — FATTA: i 4 moduli del 15/10/2026 (tutti SOLO MISURA)

Quattro moduli chiusi nella stessa sessione, **nessuno collegato al percorso
ordini**: sono strumenti di misura e di proposta. La regola di promozione dello
stadio 0 vale per tutti — nessuno di questi tocca EV, stake o gate.

| modulo | file | test | cosa aggiunge | cosa NON fa |
|---|---|---|---|---|
| **M1 (rho stimato)** | `dixon_coles.py` (608 r) | **52** | stima **MLE pesata nel tempo** del parametro Dixon-Coles `rho` sui `match_results` reali, con il time-decay dei rating | non cambia il `RHO = -0.15` di produzione: lo stimatore e' pronto, il blocco che lo **legge** non e' scritto |
| **M5 (Markov tennis)** | `tennis_markov.py` (725 r) | **66** | catena esatta punto -> game -> set -> match, con deuce, tie-break a 7 e rotazione del servizio; probabilita' **LIVE** a qualunque punteggio | non e' alimentato da `spw/rpw` misurati (serve una fonte di statistiche di servizio): resta una riparametrizzazione dichiarata |
| **M6 (drawdown)** | `drawdown_rebalancer.py` (480 r) | **45** | curva di riduzione a **piu' tier** sopra il drawdown rolling, con pavimento che **blocca** invece di ordinare un importo inutile | non sostituisce `adaptive_staking.drawdown_factor` (esposto come `factor_legacy` per il confronto) ne' gli stop giornaliero/settimanale: **ridimensiona**, non blocca |
| **M6 (risk parity)** | `risk_parity.py` (737 r) | **39** | covarianza **storica** per mercato/sport dalle `bets` saldate + criterio **ERC** (equi-risk-contribution) sul portafoglio aperto | non tocca il valore atteso: agisce **solo sulla dimensione**, e il moltiplicatore e' in [0, 1] |

**Direzione unica dei tre moduli di rischio** (`drawdown_rebalancer`,
`risk_parity`, come `copula_portfolio`): il moltiplicatore sta in **[0, 1]** —
puo' solo **ridurre** o **azzerare**. Mai un fattore > 1 (alzare lo stake dopo un
drawdown sarebbe l'opposto di un controllo di rischio), mai una promozione.

**Il gancio in `poisson_engine.py` (unica modifica al codice di produzione).**
`current_rho()` e il context manager `rho_value(rho)` (29 righe): la griglia dei
punteggi **resta** `_probs_grid` — nessuna seconda formula — e il `rho` di
prova viene **sempre ripristinato**, anche se il blocco solleva. E' il gancio che
`dixon_coles.fit_rho` usa per stimare senza duplicare la matematica, e che il
futuro blocco di promozione usera' per applicare lo stimato.

**Due proprieta' del motore Markov verificate con Monte Carlo INDIPENDENTE** (non
dedotte, e non con un simulatore che riusa le sue stesse funzioni):
1. `set_prob` coincide con una DP indipendente su tutta la griglia 0-7 x 0-7
   (max |diff| 0.0018 su 200k simulazioni per punto, dentro il rumore);
2. la probabilita' del set e' **invariante a chi serve** quando il totale dei
   game e' **pari**, e **dipende** da chi serve quando e' **dispari** (4-3:
   +0.060 sul match). La parita' e' una proprieta' del modello, non un caso:
   un test che pretendesse la simmetria *sempre* misurerebbe il falso.

**Un errore di metodo da non ripetere (mio).** Il primo test di simmetria era
basato su una simulazione che risolveva il **6-6 con una moneta equa** invece
che col tie-break vero: la "simmetria" che ne usciva era un artefatto del
simulatore, non del motore. Quando una proprieta' si verifica simulando, il
simulatore **non deve riusare il modello in prova**: o e' indipendente, o sta
verificando se stesso.

**Confini (tripwire nei test).** Sola lettura dove si legge il ledger
(`mode=ro`; il test tenta una `UPDATE` e pretende che fallisca), zero rete,
zero crediti, **zero ordini**, nessuna scrittura sul ledger, nessuna soglia di
rischio ricopiata (si legge dalle costanti dei moduli che la applicano), e
`import <modulo>` non carica `auto_bet`/`bot`/`tracker`.

**Stato: PROMOSSI (shadow) i due M6; gli altri due restano NON collegati.**
`dixon_coles` (rho) e `tennis_markov` **non** sono importati da `auto_bet`,
`bot`, `decision/` o `agents/`: in produzione il loro effetto sul denaro e'
**zero**. `drawdown_rebalancer` e `risk_parity` sono invece entrati nel percorso
ordini il **16/10/2026** attraverso `portfolio_sizing.py`, in modo **shadow**
(sezione 3-quinquies): misurano e loggano, gli stake NON cambiano finche' l'env
`PORTFOLIO_SIZING` non passa a `enforce`.

---

## 3-quinquies. La PROMOZIONE dei due M6: `portfolio_sizing.py` (16/10/2026)

I due moduli di rischio di portafoglio erano pronti e misurati dal 15/10 ma
**scollegati**: il multiplo di riduzione non arrivava a nessun ordine. La
promozione e' un modulo di **wiring** (`portfolio_sizing.py`), non due chiamate
sparse in `auto_bet`, per la regola di progetto "ogni regola vive in UN solo
posto": `auto_bet` ha **due** percorsi che mandano ordini reali (il giro delle
60s in FASE 2 e il dispatch T-60) e due implementazioni della stessa semantica
divergeebbero (i tre bug gravi del progetto — stop-loss fantasma 21/09, doppio
Kelly 13/09, doppio ordine 26/09 — sono tutti logica copiata in due posti).

**Perche' uno SHADOW sopra lo shadow.** I due moduli hanno gia' un `mode`
(`enforce`/`shadow`/`off`) ma il loro `shadow` **applica** comunque la riduzione
(`active` e' True): qui la promozione NON si affida al modo dei moduli. E' un
unico interruttore (`PORTFOLIO_SIZING`) che decide se i risultati vengono
**scritti** sui candidati, e in shadow i due moduli girano su **copie** dei
candidati — cosi' l'originale non puo' essere toccato nemmeno per errore:

| modo | comportamento |
|---|---|
| `off` | nessuna valutazione: nessun import dei due moduli, nessuna lettura del ledger |
| **`shadow`** (default) | i due gate VALUTANO e loggano cosa farebbero; gli stake NON cambiano |
| `enforce` | i due gate riducono davvero e scartano fail-closed chi scende sotto il **ticket minimo del MOTORE** |

**Ordine dei gate, non arbitrario**: `tetto per-ordine` -> **M6** -> `copula`.
Prima il **drawdown** ("quanto capitale sono disposto a mettere in gioco
ADESSO"), poi il **risk parity** ("come distribuirlo fra i bucket", sugli
importi sopravvissuti al taglio di salute); la copula resta l'ULTIMO passo di
sizing e nessun layer segue i due M6. Invertirli farebbe calcolare le quote di
rischio su stake che poi vengono riscalati.

**Direzione unica**: `min(stake_prima, prodotto_dei_due_moduli)` — se un domani
un modulo tornasse un moltiplicatore > 1 il valore viene riportato indietro e
l'evento e' CONTATO (`clamped`), mai nascosto.

**Fail-open dove serve, fail-closed dove conta**: modulo non importabile,
errore di valutazione, copia di lavoro non riconoscibile -> candidati
**INVARIATI** con `error` dichiarato (un guasto di telemetria non e' un rischio
misurato); stake ridotto **sotto il ticket** -> candidato **BLOCCATO** (mai un
importo diverso dalla riduzione per farlo passare — li' non c'e' nessuna
guardia sul sotto-ticket e `_live_fill` costruirebbe un ordine da zero).

**Le posizioni APERTE entrano nel calcolo**: il risk parity e' una proprieta'
del PORTAFOGLIO, non di un pick isolato. Il lettore e' `auto_bet._open_live_rows`
(l'unico punto che sa cosa e' aperto: stessa tabella e stessa condizione di
`_open_live_snapshot`, con il `mercato` che serve al bucket) e il travaso e' la
riga `mercato` aggiunta il **16/10**.

**Diagnosticabilita' in shadow**: la riga di riduzione porta il valore FINALE
(`stake_after`) **e** i passi intermedi (`steps`: drawdown -> risk parity),
perche' in shadow i verbi dei moduli non vengono scritti sui candidati veri —
senza `steps` un audit non potrebbe dire QUALE gate ha tagliato quanto.

**Verifiche**: `test_portfolio_sizing.py` **61 test, tutti OFFLINE** (modulo in
ombra con curve deterministiche: shadow non tocca nulla, enforce applica, mai
amplifica, parita' shadow/enforce sui parametri, fail-open, ticket, IaC) +
scenario **J** in `verify_guardrails.py` (SHADOW = ordine a stake pieno con la
riduzione dichiarata a log; ENFORCE = ordine ridotto; riduzione sotto ticket =
0 ordini e 0 righe; OFF = reversibile) — **A-J tutti bloccano** (exit 0).

⚠️ **In produzione girera' in shadow** finche' non si passa
`PORTFOLIO_SIZING=enforce`: la promozione serve prima a **misurare** quale
riduzione sarebbe stata applicata, esattamente come la shadow mode della catena
`decision/`.

## 3-sexies. La PROMOZIONE del rho Dixon-Coles: `dixon_coles.promote()` (16/10/2026)

La parte "rho stimato" del **modulo 1** era una misura scollegata dal motore:
la matematica Dixon-Coles c'era da sempre (`poisson_engine`, `rho` = costante
scritta a mano) e il time-decay pure (`rating_engine._weight`), ma **nessun
blocco leggeva la stima dai dati reali**. `dixon_coles.promote()` e' quel blocco,
nel percorso `fit -> salva -> (se autorizzato) applica`.

**La misura ha cambiato la decisione (ledger reale, container AMS).** La
verosimiglianza dei punteggi esatti e la calibrazione dei MERCATI non ordinano
lo stesso rho:

| rho | log-L | Brier 1X2 (holdout) |
|---|---|---|
| -0.30 | **-957.3 (massimo)** | 0.5506 |
| -0.15 (default) | -1024.8 | 0.5451 |
| **0.00** | -1052.7 | **0.5425 (migliore)** |

Da qui il **gate di calibrazione**: il rho stimato si applica solo se il Brier
1X2 **fuori campione** (30% piu' recente) non peggiora contro il default di
codice e contro `rho = 0`. Sul giro end-to-end il verdetto e' stato
`non_migliora_default` -> **motore al default, 0 cambi al denaro**, stima
salvata come serie storica.

**Default del modello in UN solo posto**: `poisson_engine.DEFAULT_RHO` (default
di CODICE) + `DEFAULT_RHO` come base di `RHO` (parametro effettivo). La stima e
il gate confrontano col default di codice anche dopo una promozione: seguire il
valore installato renderebbe il gate incapace di accorgersi di un peggioramento.

**Reversibilita'**: `DIXON_COLES_APPLY` (default OFF) accende l'applicazione,
`DIXON_COLES_ENABLED=0` spegne il job, `DIXON_COLES_CALIB_GATE=0` disattiva il
gate (sconsigliato), banda di sicurezza via `DIXON_COLES_SAFE_MIN/MAX`.

---

## 3-septies. Sessione 16/10/2026 — i tre moduli del piano "quant avanzato"

Tre cantieri avanzati nella stessa sessione, tutti con la stessa disciplina: la
misura decide, e dove la misura non esiste il modulo **rifiuta** invece di
produrre un numero. Due sono stati *promossi dentro il percorso* (il Markov come
componente di servizio di `tennis_quant`, il secondo benchmark del CLV), il
terzo e' stato **respinto dalla misura**.

### Modulo 5 — Markov tennis: ponte calibrato, superfici, e il referto che non dice i game

`tennis_markov.py` non e' piu' solo un simulatore in parallelo: e' diventato la
**componente di servizio** di `tennis_quant.evaluate`. Il motivo e' una misura:
il Poisson sui break, a parita' di tenute, e' **basso di 1.5-3.5pp** rispetto
alla catena esatta, e quel buco entrava dritto nel `p_intrinsic` (e quindi nel
gate "entrambi confermano"). Ora `_serve_prob_exact` prova la catena e la
provenienza **viaggia col numero** (`serve_engine` = `markov_chain` |
`poisson_hold_break`, con `serve_delta_pp`): un fallback silenzioso non puo'
passare per una misura migliore di quella che e'.

**Il difetto che ha reso necessario il ponte calibrato.** Il ponte
"probabilita' -> tenute" e' DERIVATO (nessuno stat provider): non porta
informazione indipendente, quindi il suo unico uso corretto e' di essere un
**cambio di variabile** dentro la catena. Con lo spread lineare NON lo era —
MISURATO: input 0.75 -> la catena restituiva **0.636 (-11.4pp)**; su 0.62
restituiva 0.620 **solo per caso**. `calibrate_hold_pair` risolve il
**differenziale** di tenuta a **media fissata dal fallback** (il livello non si
inventa) e riproduce la probabilita' d'ingresso a ~1e-7, con residuo esposto e
`bounded` fuori dal box. Una coppia **misurata** (`hold_a`/`hold_b`) non si
calibra mai.

**Superfici**: `surface_prob_offset` (grass **+0.02**, hard **0.0**, clay
**-0.025**, override `TENNIS_MARKOV_SURFACE_DELTA`) e' un **PRIORE del progetto,
non una misura** — la calibrazione lo riassorbe e l'informazione resta leggibile
in punti di probabilita' (non sparisce in silenzio).

**Il referto di tennis NON porta i game (misurato sul container, 16/10/2026).**
La validazione sul ledger funziona — le colonne vengono rilevate davvero
(`home_team`/`away_team`, `score_home`/`score_away`) e le **48 righe** di tennis
del referto vengono lette — e poi **rifiuta**: `plausible_share` **0.417**,
mediana del totale **1.0**, `semantics="unknown"`, motivo
`punteggi_non_compatibili_con_i_game`. La causa e' reale e va scritta: il
referto SX-native salva in `score_home/score_away` un **flag di vincitore
(1/0)**, non i game (una riga 19-15 arriva dal percorso esterno). Quindi
**`TENNIS_QUANT_BASE_HOLD` non e' calibrabile da questo ledger**: l'ingresso non
esiste, e il modulo lo dichiara invece di calibrare su un campione sbagliato in
modo invisibile. La porta di uscita della roadmap M5 resta quella: **spw/rpw**
(o un referto che salvi i game).

**Verifiche**: `test_tennis_markov.py` **96 test** (parita' formula chiusa vs DP,
tie-break esatto vs catena troncata, simmetria, calibrazione del ponte,
superfici, validazione sul ledger con punteggi di calcio rifiutati) +
`test_tennis_quant.py` **57** (provenienza del motore, delta, fallback). Tutti
offline.

### Modulo 2 — CLV contro DUE sharp: `clv_history.betfair_odds`

Il CLV si misura **contro un benchmark**, e due benchmark indipendenti valgono
piu' di uno: Pinnacle e' un book, Betfair Exchange EU e' un exchange P2P con
margine ~0 — **meccanismi diversi**. Un edge che batte entrambi e' molto meno
spiegabile con l'errore di una singola fonte.

- `pinnacle_oracle.sharp_odds_from_cache(home, away, book=...)`: la lettura
  cache diventa **generica sulla fonte** (una sola definizione del percorso;
  `pinnacle_odds_from_cache` resta la stessa con `book=PRIMARY_BOOK`, quindi
  `closing_line` e `steam_move` non cambiano firma) +
  `benchmark_odds_from_cache`;
- `tracker`: colonna **`clv_history.betfair_odds`** (ALTER idempotente) e
  `closing_line.benchmark_closing()` che scrive la quota di chiusura del secondo
  benchmark a T-0, con etichetta di fonte `betfair_t0`;
- in `clv_tracker` ogni trade porta `benchmark` / `clv_benchmark` /
  `benchmark_source`, e la sezione `benchmark` del riepilogo **non entra** nella
  statistica del CLV principale: un benchmark assente **non e' un benchmark a
  zero** e non deve declassare il campione.

**Esito REALE (container, 16/10/2026, sola lettura)**: 67 trade scansionati, **45
con una chiusura utilizzabile** (22 pendenti), CLV medio **+4.54%**, IC95
**[+2.98%, +6.11%]**, p ~ 0, **40/45 positivi (88.9%)**, edge minimo
distinguibile **2.18%** → `status: beating`. Per fonte: `pinnacle_t0` n=41 media
**+5.00%**, `sx` n=4 media **-0.11%** — le due chiusure NON si mescolano.
La colonna del secondo benchmark e' **0/45 popolata**: il percorso e' cablato e
non ha ancora catturato un T-0 di Betfair (e' nata con questa modifica).

**Cosa ha reso possibile la copertura**: le puntate ora portano l'**identita'
dell'evento SULLA RIGA DEL DENARO** (`bets.home/away/league/commence`, ALTER
idempotente + `auto_bet.event_identity()` come unico punto di estrazione). La
tabella `matches` viene **potata** dopo due giorni mentre una puntata resta per
sempre: senza quei campi la chiusura di una bet vecchia non era piu'
calcolabile. Lo stesso lettore autosufficiente e' stato dato a `smart_hedging`
(LEFT JOIN + COALESCE) e a `resting_orders` (la riga `RESTING_FILLED` nasce con
l'identita' dell'evento).

**Verifiche**: `test_clv_tracker.py` **83** + `test_closing_line.py` **50**,
tutti offline (DB temporaneo; la connessione di sola lettura **rifiuta** una
`UPDATE`).

### Modulo 1 — `ml_harness.py`: il boosting e' STATO RESPINTO dalla misura

Harness di confronto **walk-forward cronologico** degli sfidanti del modello ML
(`prior` = probabilita' base, `logistica` = l'incumbent numpy di
`ml_ensemble`, `boosting` = XGBoost), sulle feature storiche di
`match_features`, con pieghe temporali **contigue e senza rimescolamento**, IC95
sulla differenza di Brier e correzione di Bonferroni sui confronti multipli.

**Esito REALE (container, 16/10/2026, 4 pieghe, 15.193 righe out-of-fold):**

| modello | Brier pooled | LogLoss | AUC | calib_gap |
|---|---|---|---|---|
| `prior` | 0.24678 | 0.68668 | 0.502 | 0.0082 |
| **`logistica`** (incumbent) | **0.24017** | **0.67564** | **0.600** | 0.0113 |
| `boosting` (XGBoost) | 0.24309 | 0.68007 | 0.588 | 0.0150 |

| confronto | guadagno di Brier | IC95 | p | verdetto |
|---|---|---|---|---|
| boosting vs prior | **+0.00368** | [+0.00178, +0.00558] | 0.0001 | meglio |
| boosting vs **logistica** | **-0.00293** | [-0.00437, -0.00148] | 0.0001 | **peggio (significativo)** |

Verdetto del modulo: **`reject`** — "lo sfidante e' SIGNIFICATIVAMENTE peggiore
su: boosting_vs_logistica". Il boosting batte la baseline ingenua ma **perde
contro l'incumbent**, e l'intervallo di confidenza sta **tutto sotto zero**:
non e' rumore, e' un peggioramento. **Azione: non si cambia il modello di
produzione** — nessuna riga di `ml_ensemble` tocca la decisione per questo
esito, che resta la ragione per cui l'harness esiste (un booster piu' moderno
non e' un modello migliore: si misura).

**Una correzione necessaria al modulo 1**: `match_features.history_feature_rows`
ordinava le righe **per `match_id`**, che non e' un ordine temporale — un
walk-forward su quell'ordine sarebbe stato un falso (le pieghe non sarebbero
state "passato -> futuro"). Ora le righe portano `ts` e si ordinano su quello:
**l'ordine cronologico e' la proprieta' su cui poggia qualunque misura
walk-forward**.

**Verifiche**: `test_ml_harness.py` **50 test, offline** (pieghe contigue e
coperenti, nessun look-ahead, modello degenere dichiarato `degenerate`, campione
sotto il minimo, parita' fra i modelli sullo stesso vettore di feature, IoC).
Env `MLH_FOLDS` / `MLH_MIN_TRAIN` / `MLH_SEED` in `preserve()`.

---

## 3-octies. Sessione 17/10/2026 — M3 bayesiano, feature ELO/lega, i GAME del tennis nel referto

Tre cantieri chiusi, **tutti ancora in modalita' MISURA** (nessun percorso di
produzione importa questi moduli: `match_features` e' importato solo da
`ml_harness`, `bayes_ratings` solo dai suoi test — verificato con `grep`).

### M3 — `bayes_ratings.py`: inferenza gerarchica con shrinkage empirico-Bayes

Risponde alla domanda che il progetto si e' posto dal 14/10: **quanto** del
rating di una squadra e' misura e quanto e' prior? Il modulo stima i parametri
con discesa del gradiente (numpy, nessuna dipendenza nuova), poi applica lo
**shrinkage empirico-Bayes**: `sigma` per lega stimato dai dati (con pavimento e
soffitto, e un **seme largo** nel primo stadio — senza, il primo stadio collassava
sul pavimento e i rating non si muovevano).

- `fit()` / `fit_ledger()` (sola lettura `mode=ro` su `match_results`),
  `lambdas_for_match()`, `walk_forward()` (pieghe **cronologiche**), `save`/`load`
  (`BAYES_STATE`, volume) e report/CLI.
- **Il parametro nuovo e' l'INCERTEZZA**: ogni lambda porta con se' la varianza
  del posterior inattaccabile (`sigma_state`), quindi una squadra con 3 match
  visti non ha lo stesso peso di una con 30.
- Fail-closed dove serve: target/parametri impossibili ricadono sul default
  **dichiarato** (mai un valore assurdo silenzioso).

### M1 — feature nuove: ELO di calcio pre-kickoff + contesto di lega

`FEATURE_NAMES` passa da 25 a **34** feature, tutte **leak-free** per costruzione
e tutte dentro `HISTORY_FEATURES` (nessuna delle nuove finisce in
`LOOKAHEAD_UNSAFE`):

| gruppi nuovi | feature |
|---|---|
| Elo di CALCIO (formula World Football Elo, UNICA definizione) | `elo_home`, `elo_away`, `elo_diff`, `elo_expect_home` |
| contesto di lega | `league_goal_avg`, `league_prev_matches` |

Perche' l'Elo vive qui e non in un modulo condiviso: in casa esiste l'ELO
**tennis** (`tennis_sandbox`, superficie-specifico, seminato dal mercato) e
nessun Elo di calcio; l'unica alternativa era ClubElo via `live_intel`
(provider ESTERNO, non interrogabile offline e gia' caduto il 08/10). L'Elo si
aggiorna nella **passata cronologica unica** di `history_feature_rows`, quindi
un match del 2023 non vede mai un aggiornamento del 2024 (tripwire dedicato).

### M1 — il harness e' stato riesercitato con le feature nuove

`ml_harness.run()` (prior / logistica / boosting, pieghe cronologiche,
confronto appaiato via `significance.roi_test`) e' stato fatto girare **sui dati
sintetici** in locale per provare che la catena completa (feature nuove ->
matrice -> tre stimatori -> verdetto) regge dopo l'estensione di `FEATURE_NAMES`:

```
righe 5994 | out-of-fold 3994 | motore boosting: xgboost
  prior      brier=0.2501 auc=0.4987
  logistica  brier=0.1426 auc=0.8827
  boosting   brier=0.1471 auc=0.8709
  -> boosting_vs_prior     gain=+0.10294 p=0.0    better
  -> boosting_vs_logistica gain=-0.00452 p=0.0047 worse
verdetto: reject — lo sfidante e' SIGNIFICATIVAMENTE peggiore su boosting_vs_logistica
```

Le tre letture che contano: (a) le feature nuove **portano segnale** (AUC 0.88
su un processo generativo la cui forza e' latente); (b) il confronto **discrimina**
e sa dire `reject`; (c) il generatore e' lineare per costruzione, quindi il
risultato corretto e' logistica >= boosting — il harness **non e' tarato a
favore del boosting** (il verdetto del 15/10 sul dato reale era gia' `reject`,
questo lo conferma da un'altra direzione).

### M5 — i GAME del tennis arrivano nel referto (e la calibrazione si sblocca)

Il referto SX-native salva per il tennis il punteggio **canonico** `(1,0)`/`(0,1)`
preso dal campo `outcome` dell'exchange: e' la scelta giusta per il VERDETTO (i
game possono pareggiare con un vincitore — Giron-Baez 13-13 con `outcome` 2) ma
buttava via i game VERI che la stessa risposta porta in
`teamOneScore`/`teamTwoScore`. Senza quelli `tennis_markov.ledger_games_stats`
vedeva solo totali da 1 game, rispondeva `semantics="unknown"` e la calibrazione
di `BASE_HOLD` **non poteva partire** (misurato: `plausible_share` 0.417).

Cosa e' stato fatto, senza toccare il percorso del denaro:
- **`match_results` ha quattro colonne di DETTAGLIO** (`detail_home`, `detail_away`,
  `detail_kind`, `detail_source`) con ALTER **idempotente** e `COALESCE` in
  UPDATE: una ri-scrittura senza dettaglio (il watchdog ri-salva spesso) **non
  cancella** i game gia' registrati. `score_home`/`score_away`/`result` restano
  intatti (il verdetto non cambia di una virgola);
- `sx_signals` popola il dettaglio dalla risposta `markets/find` quando la
  semantica e' `games`;
- `tennis_markov.ledger_games_stats` legge il dettaglio (e dichiara la fonte del
  campione: `ledger_detail`, con `ledger` — punteggi canonici — solo come
  ripiego) e `calibrate_hold_pair` resta **fail-closed**: senza game reali la
  calibrazione e' **rifiutata**, non stimata su numeri falsi.
- **Backfill** delle righe gia' saldate senza dettaglio (CLI) e test dedicati
  (`test_tennis_games.py`): registrazione, non-invenzione (`NULL` resta `NULL`,
  `no_row` per le partite assenti dal referto), lettura del campione,
  nessuna regressione nel percorso del denaro (una bet tennis resta `won` con il
  profit invariato).

**Il numero che chiude il cerchio**: i game reali dai referti (e il backfill)
rendono `BASE_HOLD` **calibrabile** — la calibrazione va poi **misurata sul
container** (17k righe di referto), perche' in locale `match_results` e' vuoto.

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
(**84 test, tutti offline**) — **collegato** al percorso ordini LIVE.
⚠️ Il conteggio era scritto **93** e il commit del 15/10 (`a7f2121`) l'aveva
corretto a 84 **in `AGENTS.md`, non qui**: due file, due numeri. Verificato il
15/10/2026 con `pytest --collect-only -q` (84 test raccolti). La regola che ne
deriva: **un numero in due documenti e' due numeri** — si corregge dove si
dichiara, e il conteggio si prende dal comando, non dalla memoria.

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
| 6 — Portfolio theory | **Fase 1 FATTA** (misura) + **Fase 2 FATTA** (`copula_portfolio` in ENFORCE) + **Fase 3/4 FATTE e promosse in SHADOW** (`risk_parity`, `drawdown_rebalancer` via `portfolio_sizing`, § 3-quinquies) | `PORTFOLIO_SIZING=enforce` dopo aver letto i log shadow |
| 1 — ML/feature | **Fase 1 FATTA** (28 feature, 10 sopra soglia su 17k partite) + **rho Dixon-Coles promosso in misura** (`dixon_coles.promote()`, § 3-sexies) + **harness walk-forward fatto** (§ 3-septies: boosting **RESPINTO**) + **34 feature** con ELO di calcio pre-kickoff e contesto di lega, harness **riesercitato** (§ 3-octies) | rilanciare il harness **sui 17k match reali** con le feature nuove: e' l'unica cosa che puo' riaprire la porta al boosting, e il gate e' gia' scritto (Bonferroni + AUC + effetto minimo distinguibile) |
| 3 — Bayesiano | **Fase 1 FATTA** (`bayes_ratings.py`: fit gerarchico, shrinkage EB, walk-forward, persistenza sul volume) | misurare sul container (`fit_ledger` sui 17k match reali + walk-forward bayesiano vs `dixon_coles`); poi decidere se alimentare `rating_engine` |
| 2 — Line movement | **completo** + **CLV contro DUE sharp** (§ 3-septies: `pinnacle_t0` + `betfair_t0`) | M2-2: segmentazione CLV steam/non-steam |
| 5 — Markov tennis | ELO+Poisson presenti + **catena esatta promossa come componente di servizio** di `tennis_quant` (§ 3-septies) + **i GAME arrivano nel referto** (`match_results.detail_*`, § 3-octies): la calibrazione non e' piu' strutturalmente impossibile | backfill dei game sui referti vecchi + calibrazione di `BASE_HOLD` **misurata sul container**, poi confronto del modello calibrato contro la versione a prior fisso |
| 4 — Lineup window | finestra presente | rinviato fino a volume di candidati |
