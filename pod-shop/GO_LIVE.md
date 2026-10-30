# GO LIVE — attivazione del negozio Hoplix

Questo documento contiene **solo i passi che richiedono te**. Tutto il resto
(controlli sui file, testi, prezzi, margini, payload, campagne, verifica del
costo base reale) lo eseguo io e ti riporto l'output.

**Costo totale di questi passi: 0 EUR.** Nessun canone, nessuna carta, nessuna
pubblicità a pagamento.

Regola unica da rispettare: **le credenziali non si incollano in chat**. Le
scrivi tu in un file, io le leggo da lì e verifico con un'impronta (sha256)
senza mai stamparle.

---

## Passo 0 — Cosa ti serve avere a portata di mano (2 minuti)

- Codice fiscale (obbligatorio per la fatturazione).
- IBAN del conto su cui vuoi ricevere i pagamenti.
- Se hai già un'attività: P.IVA. Se non ce l'hai, leggi il riquadro "Tasse" in
  fondo a questo documento **prima** di aprire lo store: è l'unica cosa che
  nessun software può decidere al posto tuo.

---

## Passo 1 — Account e negozio gratuito su Hoplix (10 minuti, tu)

1. Apri **https://www.hoplix.com** e registrati.
2. Nel menu del venditore scegli **Store gratuito** (o "Crea il tuo store"):
   nessun canone e nessuna carta richiesta. Il dominio sarà del tipo
   `https://<qualcosa>.hoplix.shop`.
3. Annota l'indirizzo esatto del negozio: lo userò per il pulsante "Acquista"
   del sito (`NEXT_PUBLIC_SHOP_URL`).

**Criterio di completamento:** il negozio esiste e si apre in una scheda del
browser, anche vuoto.

---

## Passo 2 — Dati fiscali e pagamenti (10 minuti, tu)

Nel pannello Hoplix:

1. **Impostazioni → Dati fiscali**: nome, cognome, CF (o P.IVA se ce l'hai),
   indirizzo di residenza, IBAN.
   Senza questi dati **i pagamenti non vengono liquidati**: è il blocco numero
   uno di tutti i negozi print-on-demand.
2. **Impostazioni → Spedizioni**: verifica le tariffe mostrate. Atteso:
   Italia **4,95 EUR**, resto UE intorno a **9,95 EUR**. Se i valori sono
   diversi, scrivimelo: il prezzo di 24,99 EUR è calcolato con la spedizione a
   carico del cliente, quindi un cambio di tariffa non tocca il margine, ma
   tocca la conversione (un cliente che paga 9,95 di spedizione su una
   maglietta da 24,99 spesso abbandona).

**Criterio di completamento:** il pannello non mostra più avvisi "dati
incompleti" e la sezione pagamenti risulta attiva.

---

## Passo 3 — Credenziali API (5 minuti, tu)

1. Nel pannello Hoplix cerca **API** (di solito in Impostazioni/Integrazioni):
   genera una coppia **API key** e **Secret**.
2. Scrivile **tu** in `/home/siryo/quotaverace/.env` (il file è gitignored e
   non finisce mai in un commit), in questo formato — senza spazi e senza
   virgolette:

   ```
   HOPLIX_API_KEY=...
   HOPLIX_SECRET=...
   ```

   Se preferisci non toccare il file: da terminale, in modo che il valore non
   resti nella cronologia dei comandi,

   ```bash
   read -rsp "HOPLIX_API_KEY: " K && printf 'HOPLIX_API_KEY=%s\n' "$K" >> /home/siryo/quotaverace/.env
   read -rsp "HOPLIX_SECRET: " S && printf 'HOPLIX_SECRET=%s\n' "$S" >> /home/siryo/quotaverace/.env
   ```

3. Dimmi solo "fatto". Non incollare i valori: verifico io.

**Criterio di completamento:** le due variabili esistono nel `.env` e la
chiamata `/product-list` risponde. Lo verifico con:

```bash
cd pod-shop && ../venv/bin/python publish_hoplix.py --check
```

Atteso: costo base reale della maglietta, area di stampa, taglie, colori e
**margine al prezzo di listino**. Se il costo base è 7,50 EUR (e non 7,90) il
margine sale: il codice usa sempre il valore peggiore finché non legge il vero.

---

## Da qui in poi lavori solo tu una volta: le 5 schede dello store

L'API Hoplix crea **campagne** e **ordini**, non prodotti dello store. I cinque
prodotti vanno inseriti dall'interfaccia: è un copia-incolla, con il documento
già pronto (`pod-shop/output/STORE_SETUP.md`, generato dai file reali).

Prima di iniziare ti confermo che le schede sono allineate ai file veri:

```bash
cd pod-shop && ../venv/bin/python catalog.py                 # tabella schede
cd pod-shop && ../venv/bin/python catalog.py --write-store --write-webapp
```

**Criterio di completamento:** cinque prodotti pubblicati nel negozio, ognuno
con titolo, descrizione, tag e prezzo del documento.

---

## Cosa eseguo io, appena i tre passi sono fatti

Tutto offline o in sola lettura, nell'ordine, con output riportato:

| # | comando | cosa dimostra | esito atteso |
|---|---|---|---|
| 1 | `../venv/bin/python doctor.py` | file, aree di stampa, testi, termini protetti, legame file→capo | `0 errori` |
| 2 | `../venv/bin/python publish_hoplix.py --check` | **costo base reale** dall'API e margine ricalcolato | margine ≥ 8 EUR |
| 3 | `../venv/bin/python publish_hoplix.py` | payload esatti delle campagne (dry-run) | 5 schede `[OK]` |
| 4 | `../venv/bin/python hoplix.py --orders` | stato ordini e tracking | `nessun ordine` |
| 5 | `publish_hoplix.py --live` | crea le campagne e stampa un link per design | 5 link pubblici |

Il punto 2 è quello che risponde alla tua domanda sul **margine reale**: il
calcolo di 12,58 EUR usa 7,90 EUR di costo (IVA inclusa) e toglie il 22%. Se
l'API restituisce un costo diverso, il numero vero è quello dell'API — e te lo
dico con il conto rifatto, non con una stima.

**Test senza addebiti.** L'API ha un test mode (`HOPLIX_TEST_MODE=1`, default):
un ordine con `"test": true` attraversa tutti i controlli, calcola totale e
spedizione, e non produce nulla. Lo eseguo **solo su tua richiesta esplicita**
(`--test-order`), perché un ordine di test può comparire nel pannello come
bozza: meglio un test voluto che un rumore inatteso.

---

## Le domande a cui rispondo con i numeri, non con le impressioni

| domanda | come la misuro | dove sta il risultato |
|---|---|---|
| il margine 12,58 è vero? | costo base dall'API + IVA tolta dal prezzo | `publish_hoplix.py --check` |
| le spedizioni erodono il margine? | la spedizione la paga il cliente: confronto prezzo finale | `platforms.py --price 24.99` |
| i design sono originali? | audit contro la lista dei marchi + nessun logo/UI reale nei testi | `doctor.py` (sezione IP) |
| il sito è pronto per il telefono? | build, peso delle immagini, viewport | `cd webapp && npm run build` |
| il collegamento Hoplix funziona? | `/product-list`, payload dry-run, campagna reale | punti 2-5 |
| ordini e tracking? | `hoplix.py --orders` con stati e codici | punto 4 |

---

## Tasse — la parte che nessun tool risolve

Il negozio gratuito **fattura al cliente finale**: tu incassi il profitto da
Hoplix. Questo significa che:

- Hoplix ti paga un **compenso**, non un rimborso: va dichiarato.
- Attività **occasionale** (pochi pezzi, senza organizzazione) e **P.IVA**
  (attività continuativa) sono due regimi diversi, e la soglia non è un numero
  che si inventa: dipende dalla tua situazione.
- Senza dati fiscali il pannello **non liquida**: è l'unico blocco tecnico.

Non ti do un parere fiscale: ti dico che i 12,58 EUR sono al lordo delle
imposte sul reddito, e che il conto va fatto con il tuo commercialista prima di
puntare a centinaia di pezzi. Su 5-20 magliette al mese il numero cambia poco;
su 100 cambia molto.

---

## Cosa NON faccio, nemmeno se me lo chiedi in modo implicito

- **Non spendo denaro**: nessun annuncio, nessun tool a pagamento, nessun
  dominio nuovo. Se una cosa costa, la propongo e aspetto un sì esplicito.
- **Non compro follower, recensioni o traffico.**
- **Non dichiaro vendite che non ci sono**: visite, clic e pin non sono
  ordini, e nei report restano separati.

---

## Ordine dei passi, in una riga

1. Tu: negozio gratuito su Hoplix.
2. Tu: dati fiscali e IBAN.
3. Tu: due credenziali API nel `.env`.
4. Io: `doctor.py` → `--check` → dry-run → campagne.
5. Tu: incolli le 5 schede dello store dal documento pronto.
6. Io: SEO, contenuti social, nuove idee, monitoraggio. Tu: nessuna spesa.
