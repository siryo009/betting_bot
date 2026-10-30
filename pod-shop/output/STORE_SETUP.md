# Store Hoplix — inserimento schede

Generato da `catalog.py` sui dati veri del CSV. Non correggere a mano: rigenera.

- schede da inserire: **5**
- prezzo per maglietta: **24.99 EUR** (spedizione a carico del cliente, 4,95 EUR in Italia)
- margine per pezzo: **12.58 EUR** (costo 7.90 IVA incl., IVA 22% tolta dal prezzo)

## Perche' si inseriscono a mano

L'API Hoplix crea **campagne** e **ordini**, non prodotti dello store: e' scritto
nella documentazione ufficiale. Chi promette il contrario non l'ha letto.
Lo store gratuito si attiva dal sito Hoplix; qui sotto c'e' ogni valore da copiare.

## Prima di iniziare (una volta sola)

1. Crea l'account su hoplix.com e apri lo **Store gratuito** (nessun canone, nessuna carta richiesta).
2. In *Impostazioni > Dati fiscali* inserisci i tuoi dati: Hoplix fattura al cliente finale,
   tu fatturi a Hoplix il profitto. Senza dati fiscali i pagamenti non vengono liquidati.
3. Imposta la spedizione: **Italia 4,95 EUR**, **UE ~9,95 EUR** (verifica nella tua area).
   La spedizione la paga il cliente: non entra nel margine.
4. Collega il dominio (facoltativo): `shop.<tuo-dominio>` — su un negozio gratuito il dominio
   personalizzato si configura dal pannello; se chiede un piano, resta sull'indirizzo Hoplix.

## Le schede

### 1. CTRL + ALT + REGALA

| campo | valore |
|---|---|
| Nome | `Regalo Tecnologico Divertente | CTRL + ALT + REGALA T-shirt da Programmatore` |
| Prezzo | `24.99 EUR` |
| Colore | `white` |
| Taglie | `S, M, L, XL, 2XL` |
| Prodotto | `maglietta-unisex` |
| Immagine fronte | `/home/siryo/quotaverace/redbubble-tech-gift-2026-work/output/upload/design/ctrl-alt-regala/tee-ctrl-alt-regala.png` |
| Margine atteso | `12.58 EUR` (50% del prezzo) |

Descrizione (copia il blocco sotto, e' gia' quella del CSV):

```
Maglietta unisex con stampa CTRL + ALT + REGALA, disegnata da zero: nessun logo, nessun marchio, nessuna interfaccia reale.

PER CHI E'
Per chi passa la giornata tra tastiera, scadenze e regali dell'ultimo minuto. Funziona come regalo di Natale per un collega, per un amico informatico o per se stessi.

DETTAGLI
- Stampa digitale diretta (DTG) su maglietta unisex.
- Taglie dalla S alla 2XL.
- Disponibile sia su capo chiaro sia su capo scuro: l'inchiostro cambia colore per restare leggibile.
- Prodotto e spedito da un laboratorio di stampa in Europa.

SPEDIZIONE
Produzione in 2-5 giorni lavorativi, poi consegna. Ricevi il codice di tracciamento appena il pacco parte.

CURA
Lavare a rovescio a 30 gradi, non usare candeggina, stirare solo sul rovescio.
```

> **Attenzione**: file piu' stretto dell'area: ~6.2% di margine laterale (nessun taglio)

### 2. 404 — REGALO NON TROVATO

| campo | valore |
|---|---|
| Nome | `Regalo per Informatici | 404 REGALO NON TROVATO T-shirt Divertente per Natale` |
| Prezzo | `24.99 EUR` |
| Colore | `white` |
| Taglie | `S, M, L, XL, 2XL` |
| Prodotto | `maglietta-unisex` |
| Immagine fronte | `/home/siryo/quotaverace/redbubble-tech-gift-2026-work/output/upload/design/404-regalo-non-trovato/tee-404-regalo-non-trovato.png` |
| Margine atteso | `12.58 EUR` (50% del prezzo) |

Descrizione (copia il blocco sotto, e' gia' quella del CSV):

```
Maglietta unisex con stampa 404 REGALO NON TROVATO. Illustrazione originale: nessun logo, nessuna interfaccia reale, nessun marchio di terze parti.

L'IDEA
Errore 404: il regalo perfetto non e' stato trovato. Ma il design si'. Una battuta che capiscono subito chi passa le giornate tra browser, bug e ricerche dell'ultimo minuto.

DETTAGLI
- Stampa digitale diretta (DTG) su maglietta unisex.
- Taglie dalla S alla 2XL.
- Versione per capo chiaro e per capo scuro.
- Prodotto e spedito da un laboratorio di stampa in Europa.

SPEDIZIONE
Produzione in 2-5 giorni lavorativi, poi consegna con codice di tracciamento.

CURA
Lavare a rovescio a 30 gradi, non usare candeggina, stirare sul rovescio.
```

> **Attenzione**: file piu' stretto dell'area: ~6.2% di margine laterale (nessun taglio)

### 3. CARICA AL 100%

| campo | valore |
|---|---|
| Nome | `Regalo per Chi E' Sempre Online | CARICA AL 100% T-shirt Tech per Natale` |
| Prezzo | `24.99 EUR` |
| Colore | `white` |
| Taglie | `S, M, L, XL, 2XL` |
| Prodotto | `maglietta-unisex` |
| Immagine fronte | `/home/siryo/quotaverace/redbubble-tech-gift-2026-work/output/upload/design/carica-al-100/tee-carica-al-100.png` |
| Margine atteso | `12.58 EUR` (50% del prezzo) |

Descrizione (copia il blocco sotto, e' gia' quella del CSV):

```
Maglietta unisex con stampa CARICA AL 100%. Design vettoriale originale disegnato da zero, senza marchi e senza icone di sistema copiate.

L'IDEA
Batteria piena, feste lunghe. Per chi vive con il caricatore in borsa e arriva a Natale al 100%.

DETTAGLI
- Stampa digitale diretta (DTG) su maglietta unisex.
- Taglie dalla S alla 2XL.
- Disponibile su capo chiaro e su capo scuro.
- Prodotto e spedito da un laboratorio di stampa in Europa.

SPEDIZIONE
Produzione in 2-5 giorni lavorativi, poi consegna con codice di tracciamento.

CURA
Lavare a rovescio a 30 gradi, non usare candeggina, stirare sul rovescio.
```

> **Attenzione**: file piu' stretto dell'area: ~6.2% di margine laterale (nessun taglio)

### 4. MODALITA' AEREO

| campo | valore |
|---|---|
| Nome | `Regalo Tech per Staccare | MODALITA' AEREO T-shirt Viaggio e Digital Detox` |
| Prezzo | `24.99 EUR` |
| Colore | `white` |
| Taglie | `S, M, L, XL, 2XL` |
| Prodotto | `maglietta-unisex` |
| Immagine fronte | `/home/siryo/quotaverace/redbubble-tech-gift-2026-work/output/upload/design/modalita-aereo/tee-modalita-aereo.png` |
| Margine atteso | `12.58 EUR` (50% del prezzo) |

Descrizione (copia il blocco sotto, e' gia' quella del CSV):

```
Maglietta unisex con stampa MODALITA' AEREO. Illustrazione originale con telefono, aereo e interruttore: nessun logo, nessun marchio.

L'IDEA
La modalita' piu' sottovalutata delle feste: staccare. Un regalo tech che parla anche di silenzio e tempo libero.

DETTAGLI
- Stampa digitale diretta (DTG) su maglietta unisex.
- Taglie dalla S alla 2XL.
- Versione per capo chiaro e per capo scuro.
- Prodotto e spedito da un laboratorio di stampa in Europa.

SPEDIZIONE
Produzione in 2-5 giorni lavorativi, poi consegna con codice di tracciamento.

CURA
Lavare a rovescio a 30 gradi, non usare candeggina, stirare sul rovescio.
```

> **Attenzione**: file piu' stretto dell'area: ~6.2% di margine laterale (nessun taglio)

### 5. SISTEMA AGGIORNATO

| campo | valore |
|---|---|
| Nome | `Regalo per Programmatore | SISTEMA AGGIORNATO T-shirt Minimal per Natale` |
| Prezzo | `24.99 EUR` |
| Colore | `white` |
| Taglie | `S, M, L, XL, 2XL` |
| Prodotto | `maglietta-unisex` |
| Immagine fronte | `/home/siryo/quotaverace/redbubble-tech-gift-2026-work/output/upload/design/sistema-aggiornato/tee-sistema-aggiornato.png` |
| Margine atteso | `12.58 EUR` (50% del prezzo) |

Descrizione (copia il blocco sotto, e' gia' quella del CSV):

```
Maglietta unisex con stampa SISTEMA AGGIORNATO. Design originale con frecce di aggiornamento, regalo e barra al 100%: nessun logo, nessuna interfaccia reale.

L'IDEA
Versione festiva installata. Per chi ama il software pulito e le feste senza bug.

DETTAGLI
- Stampa digitale diretta (DTG) su maglietta unisex.
- Taglie dalla S alla 2XL.
- Disponibile su capo chiaro e su capo scuro.
- Prodotto e spedito da un laboratorio di stampa in Europa.

SPEDIZIONE
Produzione in 2-5 giorni lavorativi, poi consegna con codice di tracciamento.

CURA
Lavare a rovescio a 30 gradi, non usare candeggina, stirare sul rovescio.
```

> **Attenzione**: file piu' stretto dell'area: ~6.2% di margine laterale (nessun taglio)

## Regola dell'inchiostro (la piu' facile da sbagliare)

Le schede `light` usano l'immagine **a inchiostro scuro** e vanno su capo **bianco**.
Le schede `dark` usano l'immagine **a inchiostro chiaro** e vanno su capo **nero**.
Non unire i due colori in una scheda sola: la campagna ha una sola immagine fronte e
una stampa nera su maglietta nera non si vede (e un reso per questo si paga tutto).

## Controllo prima di salvare ogni scheda

- anteprima del mockup: la stampa deve stare dentro l'area, non sul bordo;
- il prezzo mostrato deve essere quello della tabella (Hoplix rifiuta un prezzo
  sotto il costo base, quindi se lo accetta il margine c'e');
- le taglie visibili devono essere S, M, L, XL, 2XL;
- la descrizione non deve contenere prezzi o promesse di spedizione diverse da quelle reali.
