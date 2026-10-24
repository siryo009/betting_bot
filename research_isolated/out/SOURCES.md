# Fonti consultate — data di consultazione: 20/10/2026

Le fonti esterne sono state usate solo per **inquadrare** le famiglie di strategie e per capire
quali prove esistano in letteratura. **Nessun numero di questo progetto viene da una fonte
esterna**: tutti i numeri del report sono calcolati sullo snapshot del ledger di produzione
(§ "Fonti interne"). Dove una fonte esterna afferma qualcosa che i nostri dati non permettono di
misurare, il report lo marca `NON VERIFICABILE` invece di importare la conclusione.

## 1. Closing Line Value (famiglia 1 e 2)

| fonte | link | cosa se ne ricava | tipo |
|---|---|---|---|
| OddsIndex — *Closing Line Value: Beat the Market* | https://oddsindex.com/guides/closing-line-value-guide | definizione operativa di CLV e perché è usato come test di edge | guida divulgativa |
| BetHero — *What Is CLV in Betting?* (26/09/2026) | https://betherosports.com/blog/closing-line-value-explained | afferma che battere la closing di 2–3% su grandi campioni è predittivo | blog, non peer-reviewed |
| Unabated — *Closing Line Value Calculator* | https://unabated.com/tools/calculators/closing-line-value | formula del CLV e uso come metrica di processo | strumento/guida |
| The Power Rank — *Closing line value* (29/07/2021) | https://thepowerrank.com/2021/07/29/closing-line-value/ | **avverte** che CLV non implica automaticamente profitto: dipende dalla forza del mercato | analisi, utile come contro-argomento |
| Sharp Football Analysis — *What is CLV Betting* (28/10/2025) | https://www.sharpfootballanalysis.com/sportsbook/clv-betting/ | panoramica del CLV betting nel 2025-26 | guida divulgativa |

**Uso nel report:** la famiglia 1 è classificata `PROMETTENTE` **non** perché queste fonti lo
dicano, ma perché sui nostri dati il CLV contro Pinnacle è +3.34% (n=40, CI 1.90–4.88) — e resta
`PROMETTENTE` e non `VALIDATO` proprio perché il P/L non lo conferma.

## 2. Modelli statistici (famiglia 3)

| fonte | link | cosa se ne ricava |
|---|---|---|
| Dixon M.J., Coles S.G. (1997), *Modelling Association Football Scores and Inefficiencies in the Football Betting Market*, JRSS-C 46(2):265–280 | https://academic.oup.com/jrsssc/article-abstract/46/2/265/6990546 · PDF: https://www.ajbuckeconbikesail.net/wkpapers/Airports/MVPoisson/soccer_betting.pdf | il modello di riferimento (Poisson bivariato con correzione dei punteggi bassi) e l'ipotesi di inefficienza del mercato |
| Lancaster University eprint | https://eprints.lancs.ac.uk/id/eprint/19492/ | riferimento bibliografico ufficiale |
| regista — implementazione R del modello | https://torvaney.github.io/regista/reference/dixoncoles.html | implementazione indipendente utile per confrontare la nostrа griglia |

**Uso nel report:** il nostro `poisson_engine` implementa già questa correzione; il 17/10 il
`rho` stimato sul ledger (−0.28) è stato **rifiutato dal gate di calibrazione** (Brier holdout
peggiore del default). La fonte dice *come* modellare, non che il modello batta il mercato — e
infatti non lo batte.

## 3. Machine learning (famiglia 4)

| fonte | link | cosa se ne ricava |
|---|---|---|
| Hubáček O., Šourek G., Železný F. (2019), *Exploiting sports-betting market using machine learning*, Int. J. Forecasting 35(2):783–796 | https://ideas.repec.org/a/eee/intfor/v35y2019i2p783-796.html · PDF: http://ida.felk.cvut.cz/zelezny/pubs/ijf.2019.pdf | il profitto documentato deriva dalle **feature** e dalla selezione delle partite, non dal classificatore in sé |
| Wilkens S. (2021), *Sports prediction and betting models in the machine learning age*, J. Sports Analytics | https://journals.sagepub.com/doi/abs/10.3233/JSA-200463 | rassegna: i modelli ML battono i baseline solo con informazione aggiuntiva |
| *A Systematic Review of Machine Learning in Sports Betting* (arXiv 2410.21484) | https://arxiv.org/html/2410.21484v1 | molti risultati pubblicati non sono riproducibili fuori campione |

**Uso nel report:** coerente con la nostra misura (boosting **peggiore** della logistica:
Brier 0.2395 vs 0.2344, p=0.0001 su 15.213 righe out-of-fold). La letteratura spiega *perché*:
senza feature nuove, un modello più potente non aggiunge informazione.

## 4. Arbitraggio cross-venue (famiglia 5)

| fonte | link | cosa se ne ricava |
|---|---|---|
| Steinmetz (Union College), *Arbitrage Between Sportsbooks and Prediction Markets* (13/04/2026) | https://steinmetz.union.edu/abstract/betting-inefficiency-arbitrage-between-sportsbooks-and-prediction-markets | le opportunità esistono con ROI da frazioni di punto a ~20%, ma richiedono **due conti finanziati** e latenza |
| Liu H. (2026), *A comparison between financial and gambling markets*, SN Bus. Econ. | https://link.springer.com/article/10.1007/s43546-026-01202-9 | confronto efficienza/forze di arbitraggio fra i due mercati |
| Novig — *Best Arbitrage Betting Software in 2026* (17/09/2026) | https://novig.com/blog/best-arbitrage-betting-software-2026 | panorama degli strumenti commerciali (utile per i costi, non per la prova) |
| LSports — *How Do Sportsbooks Detect Arbitrage in 2026* | https://www.lsports.eu/blog/how-do-sportsbooks-detect-arbitrage/ | il rischio di limitazione/chiusura del conto è parte del costo |

**Uso nel report:** la famiglia è `SCARTATO` non per mancanza di letteratura, ma perché sui
nostri dati **l'arbitraggio intra-SX non esiste** (overround interno mediano 1.75%, 0 coperture
< 1.0 su 98 partite) e quello cross-venue richiede un secondo conto finanziato che i vincoli
dichiarati escludono.

## 5. Prediction market e contesto regolamentare

| fonte | link |
|---|---|
| Stanford News — *Prediction markets are surging* (28/04/2026) | https://news.stanford.edu/stories/2026/04/prediction-markets-facts-gambling-regulation |
| Congressional Research Service — *Prediction Markets: Policy Issues for Congress* (20/03/2026) | https://www.congress.gov/crs-product/IF13187 |

Usate solo come contesto: nessuna strategia del report dipende da un quadro regolamentare diverso
da quello in cui opera oggi il progetto (exchange P2P, nessun deposito presso terzi).

## 6. Fonti interne (tutti i numeri del report)

| fonte | percorso | uso |
|---|---|---|
| snapshot del DB di produzione | `research_isolated/snapshot/quotaverace_prod.db` (aperto `mode=ro`) | tutte le misure del ledger |
| output del laboratorio | `research_isolated/out/STRATEGY_RESULTS.json`, `STRATEGY_COMPARISON.csv`, `run_lab.log` | numeri citati nel report |
| saldo wallet | `execution_engine.py --provider sxbet --balance` (03:25 UTC del 20/10) | capitale 21.1004 USDC |
| stato dei blocchi | `data/execution/t60_kill.json`, `daily_stop.json`, `weekly_stop.json` | CB2 armato il 18/10 04:42 su equity 23.6901 |
| costanti del progetto | `value_filter`, `market_calib`, `tennis_lane`, `auto_bet`, `decision/stake_engine` | soglie usate dalle strategie (lette, mai copiate; test dedicati) |
| stress test del capitale | `scripts/stress_test_bankroll.py` | MaxDD p95 e probabilità di restare senza capitale operativo |

## 7. Cosa **non** ho potuto verificare (e non ho inventato)

- **Nessuno studio peer-reviewed specifico su SX Bet** come exchange calcistico: le ricerche
  hanno restituito materiale su Betfair e su prediction market. Le conclusioni su SX vengono
  dalle **nostre** misure (overround, liquidità, fill rate).
- **Fonti in cinese/altre lingue**: le ricerche in inglese hanno saturato i risultati utili; non
  ho trovato letteratura non anglofona che aggiungesse evidenza misurabile sul nostro caso. Se
  servisse, la ricerca va fatta su termini specifici (es. 亚盘/handicap asiatico, 大小球/totals)
  e con la stessa regola: nessun numero senza campione.
- **Prezzi eseguibili storici su SX**: non esistono (nessuno storico ordini lato exchange). Per
  questo il report non simula prezzi e dichiara le famiglie 2 e 6 `NON VERIFICABILE`, con il
  piano di raccolta in `IMPLEMENTATION_PLAN.md` § A2.
