# Protocollo di OSSERVAZIONE (nessun ordine reale) — congelato il 2026-10-23

Stato: kill switch `off` (override su volume), `provider_ready: true`, LIVE non
attivo. Le regole qui sotto sono scritte **prima** di raccogliere il campione e
non possono essere cambiate dopo aver visto i numeri (stessa disciplina di
`lab/clv_prereg.json`, protocollo CLV-VALIDATION-1).

## Cosa viene registrato (e da dove)

Tutto è già prodotto dai job di produzione, **a prescindere** dal kill switch:
nessun ordine, nessuna scrittura nuova, solo lettura.

| grandezza | fonte | finestra |
|---|---|---|
| quote osservate + fonte | `market_quotes` (`gateway_id`, `source`, `observed_at`) + cache `toa_*`/`toao_*` | continua |
| segnali e motivo accettazione/rifiuto | `predictions` (`status`, `ev`, `market_edge`, `league`, `created_at`) + `oracle_skips.jsonl` + `flow_measure` | continua |
| quota al momento della decisione | `predictions.quota` (prima occorrenza del segnale) | continua |
| prezzi di chiusura | `clv_history.closing_odds` (+ `betfair_odds` come secondo sharp) | T-0 `closing_line` |
| commissioni / slippage | SX = 0 commissione dichiarata; slippage misurato da `bets.price` vs `predictions.quota` e da `fill_rate` | continua |
| risultati simulati + ROI netto | `predictions` saldate (profit **per unità di stake**) → `significance`, `market_diagnose`, `multi_market.shadow_report` | continua |
| CLV | `clv_tracker` (per trade eseguiti e, con `--include-signals`, sui segnali) | continua |
| drawdown | `bankroll_history` → `auto_bet.weekly_drawdown()` | 168h rolling |
| errori settlement / duplicati / anomalie | `tracker.settlement_residue()`, gruppi `(market_id, selection_id)`, righe `FULLY_FILLED` senza `bet_id`, verdetti anomali | continua |

## Definizioni (fisse)

- **Universo dei segnali**: tutte le righe `predictions` con kickoff futuro al
  momento della nascita, **incluse** quelle `rejected` (senza gli scarti la
  selezione non è verificabile).
- **ROI**: media del P/L per unità di stake sulle righe **chiuse** (`push` = 0).
  Non è un ROI di cassa: lo stake non viene pagato, la scala non c'entra.
- **CLV**: `quota_acquisto / quota_chiusura − 1` (`market_calib.clv_raw`),
  esclusi gli "eco" (chiusura = quota d'acquisto) e le chiusure assenti.
- **Successo**: n ≥ 30 **e** limite inferiore del CI95 > 0.
  **Fallimento**: n ≥ 30 **e** limite superiore del CI95 < 0.
  Altrimenti **insufficiente** (mai promosso per direzione).
- **Cosa NON è evidenza**: ROI su meno di 30 chiusure; CLV positivo su una sola
  fonte sharp; qualunque numero letto su una finestra che include ere diverse
  senza filtro `--since`/fascia quota.

## Comandi di lettura (sola lettura, zero crediti, zero ordini)

```
python3 flow_measure.py --hours 24
python3 clv_tracker.py --days 14
python3 significance.py --all-statuses --since 2026-10-23
python3 market_diagnose.py --since 2026-10-23 --odds-min 1.30 --odds-max 2.00
python3 multi_market.py report --since 2026-10-23
python3 fill_rate.py --days 7
python3 liquidity_monitor.py --days 7
python3 order_watch.py
```

## Vincoli operativi del periodo

1. **Nessun ordine reale**: il kill switch resta `off` per tutta la finestra.
2. **Nessuna modifica al ledger** (niente cancellazioni, niente risaldi).
3. **Nessun aumento di capitale**.
4. Ogni valutazione va letta **con il suo caveat**: campione e CI95 sempre
   riportati insieme al punto di stima.
