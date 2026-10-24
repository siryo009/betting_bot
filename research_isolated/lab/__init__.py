"""Laboratorio di ricerca isolato per QuotaVerace.

Regole del laboratorio (deliberate, non negoziabili):

* SOLA LETTURA: si lavora su una COPIA del ledger di produzione presa con la
  backup API di SQLite (`research_isolated/snapshot/quotaverace_prod.db`).
  Nessun modulo qui dentro apre il DB di produzione ne' scrive su di esso.
* NIENTE RETE nel percorso di valutazione: i moduli leggono solo lo snapshot.
  Le misure live (ordini book SX) vivono in script separati e usano l'API
  PUBBLICA, senza credenziali.
* NIENTE FORMULE DI PRODUZIONE RICOPIATE: il de-vig e il CLV vengono importati
  da `market_calib`, il settlement da `tracker`, le soglie da `value_filter`.
  Se una formula vive in due posti, il progetto ha gia' imparato (13/09, 21/09,
  27/09) che le due copie divergono.
* ANTI-LEAKAGE: ogni regola usa SOLO informazioni disponibili al momento della
  decisione (`created_at`, quota, probabilita' di mercato, edge, EV). La closing
  line e' un METRICA DI PROCESSO, mai un filtro ex ante.
"""
