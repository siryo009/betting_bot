# Caccia all'edge — il tetto EV e' una regola o una fetta?

Snapshot: `/home/siryo/quotaverace/research_isolated/snapshot/quotaverace_prod.db` (quick_check **ok**, 2219 previsioni, 81 bet)

Soglie di produzione (lette da `value_filter`): quota 1.20-2.00, EV >= 1.5%, edge >= 1.0%. Test accountati: **60**, alfa Bonferroni **8.33e-04**.

## Universo (1X2 di produzione)

78 righe chiuse, ROI **+11.38%** (CI95 -5.25% .. +28.00%), hit +65.38%, quota media 1.640.

## 1) Tabella dei tetti

| tetto | n | ROI | CI95 | CI Bonferroni | esclusi: n / ROI | delta (CI95) | stato |
|---|---|---|---|---|---|---|---|
| 4% | 45 | +18.76% | -2.94% .. +39.33% | -22.34% .. +50.47% | 33 / +1.33% | +17.43% (-16.52% .. +51.85%) | DA VERIFICARE |
| 5% | 48 | +21.37% | +1.01% .. +40.44% | -15.26% .. +51.72% | 30 / -4.60% | +25.96% (-8.96% .. +61.00%) | PROMETTENTE |
| 6% | 48 | +21.37% | +1.01% .. +40.44% | -15.26% .. +51.72% | 30 / -4.60% | +25.96% (-8.96% .. +61.00%) | PROMETTENTE |
| 8% | 54 | +19.84% | +0.67% .. +37.97% | -17.86% .. +50.41% | 24 / -7.64% | +27.48% (-9.65% .. +65.53%) | PROMETTENTE |
| 10% | 59 | +19.60% | +1.63% .. +36.16% | -15.59% .. +47.39% | 19 / -14.14% | +33.74% (-6.68% .. +74.63%) | PROMETTENTE |
| 12% | 66 | +11.87% | -6.49% .. +28.95% | -16.20% .. +43.69% | 12 / +8.69% | +3.18% (-45.01% .. +52.18%) | DA VERIFICARE |
| 15% | 69 | +11.97% | -5.86% .. +29.33% | -16.75% .. +39.94% | 9 / +6.88% | +5.09% (-49.04% .. +62.52%) | DA VERIFICARE |
| 20% | 73 | +9.37% | -7.82% .. +26.52% | -20.57% .. +36.65% | 5 / +40.78% | -31.42% (-85.13% .. +43.22%) | DA VERIFICARE |
| production | 78 | +11.38% | -5.25% .. +28.00% | -16.03% .. +38.40% | 0 / n/d | n/d (n/d .. n/d) | DA VERIFICARE |

Tetti con CI Bonferroni che esclude lo zero: **0** su 9 provati (alfa 8.33e-04).

## 2) Monotonia: ROI per fascia di EV

| fascia EV | n | ROI | CI95 | CI Bonferroni | hit | quota media |
|---|---|---|---|---|---|---|
| -inf .. 2.0% | 350 | -1.26% | -8.71% .. +6.05% | -13.29% .. +11.15% | +51.71% | 1.587 |
| 2.0% .. 3.0% | 19 | +14.75% | -18.67% .. +45.36% | -43.95% .. +65.82% | +68.42% | 1.615 |
| 3.0% .. 4.0% | 7 | +37.88% | -12.19% .. +74.29% | -60.86% .. +79.13% | +71.43% | 1.669 |
| 4.0% .. 6.0% | 4 | +45.42% | +16.66% .. +67.71% | +0.00% .. +68.77% | +75.00% | 1.532 |
| 6.0% .. 10.0% | 13 | +8.98% | -34.25% .. +48.46% | -61.11% .. +70.44% | +61.54% | 1.661 |
| 10.0% .. 20.0% | 17 | -25.21% | -60.88% .. +10.29% | -78.94% .. +39.18% | +35.29% | 1.695 |
| 20.0% .. +inf | 5 | +40.78% | -31.97% .. +90.05% | -100.00% .. +96.30% | +80.00% | 1.799 |

**Verdetto monotonia: NON MONOTONO** (fasce usabili 4, rho sulle fasce -0.39999999999999997 contro soglia -0.5).

Spearman(EV, profitto) = **0.10140654076768431** CI95 [0.0040989775577930055, 0.19248891650801334] su 415 righe / 415 partite -> INFORMATIVO (rho>0).

## 3) Walk-forward del tetto (scelto solo sul passato)

14 giorni valutati, scelte: {'production': 7, '20%': 1, '8%': 1, '10%': 1, '5%': 4}.
ROI aggregato **+17.07%** (CI95 -0.92% .. +34.02%, n=63) -> **NON DIMOSTRATO FUORI CAMPIONE**.

## 4) Split d'era

| era | n prod | ROI prod | n tetto | ROI tetto | delta |
|---|---|---|---|---|---|
| -inf .. 2026-09-11 | 0 | n/d | 0 | n/d | n/d |
| 2026-09-11 .. 2026-10-14 | 24 | +24.72% | 11 | +34.73% | +10.01% |
| 2026-10-14 .. +inf | 54 | +5.45% | 37 | +17.40% | +11.94% |

## 5) Denaro reale (puntate live)

80 puntate live chiuse, 55 collegate a una previsione (`unlinked` 25 senza collegamento, 0 senza stake) -> MISURATO. Stake collegato 154.34589999999994 USDC, P/L -10.510000000000002 USDC (profitto per unita' per il ROI).

| fascia EV | n | ROI | CI95 |
|---|---|---|---|
| EV < 3% | 12 | -16.28% | -63.64% .. +33.79% |
| EV 3-6% | 21 | +21.34% | -16.84% .. +59.67% |
| EV 6-10% | 11 | -7.39% | -57.74% .. +46.60% |
| EV >= 10% | 11 | -68.80% | -100.00% .. -22.66% |

Con tetto 6%: n=33 ROI +7.66% (+7.08 USDC su 87.53) contro produzione n=55 ROI -10.64% (-10.51 USDC su 154.35).

## Verdetto

| controllo | esito | dettaglio |
|---|---|---|
| in-sample cap | OK | miglior tetto 5% n=48 ROI +21.37% |
| esclusi peggiori (contrasto) | NO | esclusi n=30 ROI -0.04595666666666666, delta CI [-0.0896095833333333, 0.6099666666666667] |
| monotonia | NO | NON MONOTONO |
| correlazione EV/profitto | NO | rho=0.10140654076768431 CI=[0.0040989775577930055, 0.19248891650801334] |
| walk-forward | NO | NON DIMOSTRATO FUORI CAMPIONE |
| regge in piu' di un'era | OK | 2 ere su 3 con delta>0 |
| denaro reale non peggiora | OK | live cap {'n': 33, 'roi': 0.07660353044744823, 'usdc': 7.08, 'stake': 87.53} vs prod {'n': 55, 'roi': -0.10641294251648277, 'usdc': -10.510000000000002, 'stake': 154.34589999999994} |

**3/7 controlli superati -> NESSUNA REGOLA: nessun tetto promosso**

## Piano (scritto, NON eseguito)

Gatante se promosso: tetto EV <= 6% sui soli 1X2 con quota 1.20-2.00, favorito di mercato.
Shadow 7 giorni con almeno 20 segnali.
- shadow: nessuna divergenza di PREZZO fra pick e ordine
- shadow: ROI delle righe selezionate con CI95 che esclude zero
- live: massimo 1.00 USDC per ordine finche' l'equity non supera 50 USDC
- live: stop se 10 puntate chiudono con P/L < -3.00 (rollback a produzione)
- live: stop se il residuo dalla riconciliazione supera 0.50 USDC/giorno

Rollback: rimuovere il tetto: e' un solo confronto (ev <= cap) nel gate.

