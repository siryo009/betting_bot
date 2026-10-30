# pod-shop — cinque magliette tech, zero abbonamenti

Cinque design gia' fatti (magliette unisex, taglie S-2XL), testi gia' scritti,
file DTG 3600x4800 gia' validati. Quello che mancava era **dove venderli senza
pagare l'apertura di un negozio**: qui c'e' la scelta, i numeri che la
sostengono, e gli strumenti per pubblicare.

## La scelta: **Hoplix**, non Printify Pop-Up Store

Non e' una preferenza, sono cinque fatti verificati sulle fonti ufficiali il
28/10/2026 (`platforms.py` li porta con se', con la fonte accanto a ogni numero).

| | Hoplix | Printify Pop-Up |
|---|---|---|
| costo t-shirt (IVA incl.) | **7,90 EUR** (catalogo API: 7,50) | ~10,50 EUR — **stima**, varia col fornitore |
| chi incassa | **Hoplix** | noi (Stripe: 1,5% + 0,25) |
| IVA / fatture | **Hoplix fattura al cliente** | a carico nostro (OSS) |
| pubblicazione via API | campagne (sì), prodotti store (no) | **no**: `publish` non agisce senza un canale supportato |
| test senza spendere | **sì** (`test: true` su ordini e campagne) | no |
| canone / fee di listing | 0 / 0 | 0 / 0 |
| produzione + consegna IT | 2-5 gg + 1-3 gg | 2-7 gg + 2-5 gg |
| catalogo | ~150 prodotti | ~2000 prodotti (unico vantaggio vero) |

**Margine per pezzo a 24,99 EUR** (`platforms.py` lo ricalcola):

    Hoplix   24,99 / 1,22 = 20,48 netti - 7,90 = 12,58 EUR   (50% del prezzo)
    Printify 24,99 / 1,22 = 20,48 netti - 10,50 - 0,62 =  9,36 EUR   (37%)

Il vantaggio di Hoplix sono **3,22 EUR per maglietta** piu' l'eliminazione di
Stripe e degli adempimenti IVA. Su 30 pezzi al mese sono ~97 EUR di differenza
con lo stesso prezzo di vetrina.

```
venv/bin/python platforms.py            # tabella completa, con punteggi e motivi
```

## Cosa e' pronto

- `LISTINGS.csv` — 5 schede: titolo, descrizione, 13 tag, immagine chiara e
  scura, prezzo minimo.
- `redbubble-tech-gift-2026-work/output/upload/design/*/tee-*.png` — file DTG
  3600x4800 (l'area Hoplix e' 320x400 mm: il file 3:4 entra con ~6% di margine
  laterale, nessun taglio — `print_area_check` lo verifica per ogni file).
- `catalog.py` — genera le 5 schede da incollare nello store e il manifest per
  il sito.
- `publish_hoplix.py` — crea le **campagne** via API (pagine di vendita con
  link condivisibile), idempotenti sul registro.
- `hoplix.py` — client API v1.1 (catalogo, campagne, ordini, tracking) con
  **test mode di default**.
- `publish.py` — il publisher Etsy/Printify resta intatto: Etsy e' in pausa,
  non cancellato.

## Lancio, in ordine

### 1. Genera il materiale (0 EUR, nessuna chiave)

```bash
cd pod-shop
../venv/bin/python catalog.py --write-store --write-webapp
```

Scrive `output/STORE_SETUP.md` (da incollare) e `webapp/content/shop.ts` +
`webapp/public/shop/*.png` (vetrina del sito). Per aggiungere le versioni per
capo scuro: `--include-dark` (raddoppia le schede: fallo dopo la prima vendita,
non prima).

### 2. Apri lo store Hoplix (0 EUR)

1. Account su hoplix.com → **Store gratuito** (nessun canone, nessuna carta).
2. *Impostazioni → Dati fiscali*: senza, i pagamenti non vengono liquidati.
3. Spedizione: Italia 4,95 EUR (a carico del cliente).
4. Inserisci le 5 schede da `output/STORE_SETUP.md` — sono copia-incolla.
   **Perche' a mano**: l'API crea campagne e ordini, non prodotti dello store.
   E' scritto nella documentazione ufficiale; chi dice il contrario non l'ha
   letta.

### 3. Campagne (facoltativo ma gratis, e sono il miglior biglietto da visita)

Servono immagini pubbliche (Hoplix scarica da URL, non dal disco):

```bash
# 1) pubblica il sito con le immagini
cd ../webapp && npm run build          # le immagini sono in public/shop/
vercel --prod                          # oppure il deploy che usi
# 2) crea le campagne
cd ../pod-shop
export HOPLIX_API_KEY=...  HOPLIX_SECRET=...        # dal pannello Hoplix
export HOPLIX_DESIGN_BASE=https://quotaverace.vercel.app/shop/design
../venv/bin/python publish_hoplix.py --check         # costo base vero + margine
../venv/bin/python publish_hoplix.py                 # dry-run: payload esatti
../venv/bin/python publish_hoplix.py --live          # crea (idempotente)
```

Ogni campagna torna un `link`: e' quello che si condivide.

### 4. Collega il negozio al sito

`webapp/app/shop/page.tsx` legge il manifest e mostra le schede; il pulsante
"Acquista" punta al negozio quando la variabile e' impostata:

```bash
# su Vercel (progetto quotaverace)
NEXT_PUBLIC_SHOP_URL=https://<tuo-negozio>.hoplix.shop
```

Senza quella variabile la voce "Shop" **non compare** nel menu e le card dicono
"In arrivo": meglio nessun link che un link a una pagina vuota. La vetrina non
vende: il pagamento avviene sul negozio, quindi nessun dato della carta passa
dal nostro sito.

### 5. Ordini e tracking

```bash
../venv/bin/python hoplix.py --orders            # stati + codici di tracking
../venv/bin/python hoplix.py --catalog           # costo base e colori reali
```

Gli ordini arrivano dal negozio e li produce/spedisce Hoplix: al venditore resta
il controllo del tracking (che e' l'unica cosa che il cliente chiede).

## Da sviluppato a venduto: la procedura

Tre documenti operativi, in ordine di lettura.

1. **`GO_LIVE.md`** — cosa spetta a te (3 passi: negozio gratuito su Hoplix,
   dati fiscali e IBAN, due credenziali API) e cosa eseguo io dopo ogni passo,
   con i comandi e l'esito atteso. Costo: zero.
2. **`doctor.py`** — controllo pre-volo in un comando: file, trasparenza,
   **leggibilita' del disegno sul capo a cui e' abbinato**, testi, termini
   protetti, provenienza vettoriale, economia, manifest del sito, peso delle
   immagini, API e ordini. Senza credenziali le sezioni che ne hanno bisogno
   sono `ATTESA`, non errori.

   La leggibilita' si misura in **due numeri**, perche' uno solo non descrive
   un disegno illustrato: `legible_pct` (quota di pixel con contrasto **≥ 4,5:1**,
   la soglia WCAG AA) e il contrasto **medio**. Sotto il **5%** leggibile il
   disegno e' di fatto svanito sul tessuto (`errore`), sotto il **12%** e' al
   limite (`avviso`); un contrasto medio sotto 1,35 significa che inchiostro e
   capo sono quasi dello stesso tono. Il colore mediano NON si usa: su un
   disegno con blocchi colorati e testo scuro descrive il blocco, non cio' che
   si legge.
3. **`marketing/`** — `SEO.md` (keyword, pagine, piano contenuti),
   `social.md` (pin Pinterest, script TikTok, caption Instagram, calendario),
   `ideas.csv` (12 idee con segnale di domanda e rischio IP),
   `GROWTH_PLAN.md` (da 5 a 30 prodotti, KPI settimanali, regole di prezzo).

```bash
../venv/bin/python doctor.py                                  # offline
../venv/bin/python doctor.py --json                           # per script
../venv/bin/python doctor.py --base https://quotaverace.vercel.app  # URL pubbliche
```

Exit `0` significa "si puo' pubblicare"; `1` significa che c'e' almeno un
errore da correggere prima di incollare le schede. La prima passata richiede
~30 secondi (dieci PNG da 3600x4800 da decodificare), le successive ~1 secondo:
le misure restano in `.doctor_cache.json`, indicizzate sulla **firma del file**
(percorso + data + dimensione), quindi un disegno rigenerato viene rimisurato
e una cache corrotta si ricalcola da sola. `DOCTOR_CACHE=0` la spegne,
`DOCTOR_CACHE=/percorso/file.json` la sposta (misurato: 36s -> 1,3s).

## Test del negozio

```bash
cd pod-shop && ../venv/bin/python -m pytest        # 239 test, ~90s, tutti offline
../venv/bin/python -m pytest test_doctor.py        # solo il pre-volo
```

`pod-shop/pytest.ini` isola questi test dal `conftest.py` del progetto di
betting che li ospita (fixture autouse pesanti) e dal plugin di SeleniumBase,
che altrimenti inietta un banner e cerca un webdriver a ogni sessione: prima di
quel file la stessa suite non finiva in tre minuti.

## Cosa e' automatico e cosa no

| passo | automatico | nota |
|---|---|---|
| testi, prezzi, margini, controllo file | **sì** (`catalog.py`, `platforms.py`) | nessuna spesa |
| schede dello store | **no** (5 inserimenti) | l'API non li crea |
| campagne + link | **sì** (`publish_hoplix.py --live`) | richiede immagini pubbliche |
| produzione e spedizione | **sì** (Hoplix) | 2-5 gg + 1-3 gg IT |
| tracking al cliente | sì, dal negozio | leggibile anche da qui |
| incasso e fattura | **sì** (Hoplix) | nessun Stripe, nessun OSS |

## Margini reali (verificati, non stimati)

| pezzo | prezzo | costo | margine | % |
|---|---|---|---|---|
| 1 maglietta | 24,99 | 7,90 | **12,58** | 50% |
| 5 pezzi | 124,95 | 39,50 | **62,90** | 50% |
| 10 pezzi | 249,90 | 79,00 | **125,80** | 50% |

La spedizione la paga il cliente (4,95 IT) e **non** entra nel margine. L'IVA e'
gia' tolta. Se il prezzo scende a 19,99 il margine scende a 8,49 (42%): sotto i
19 EUR conviene smettere di scendere, non vendere di piu'.

## Attirare clienti senza pubblicità a pagamento

Ordinate per resa reale, non per moda:

1. **Campagne condivisibili** (gratis, immediate): un link per design, da
   mettere in bio, in un commento, in un messaggio. E' il motivo per cui le
   campagne valgono il tempo di crearle.
2. **Community dove il regalo tech e' il tema** (Reddit r/ItalyInformatica,
   r/programmatori, gruppi Facebook di sviluppatori, Discord di corsi): si
   partecipa e si mostra il design **solo dove le regole lo consentono** —
   spam = ban, e il ban costa piu' di un annuncio.
3. **Pinterest**: una grafica per design, titolo con "regalo per programmatore".
   Traffico che continua per mesi, zero budget.
4. **TikTok/Reels organici**: 10 secondi di "errore 404: regalo non trovato".
   I design sono gia' battute: il video e' il mockup + il testo.
5. **SEO sul sito**: la pagina `/shop` e le schede hanno titolo e descrizione
   veri (`metadata`), quindi indicizzabili; il marchio "regalo tecnologico
   natale 2026" e' a bassa concorrenza.
6. **Cross-listing** sui marketplace che non chiedono apertura (Redbubble,
   Teepublic): gli stessi file sono gia' pronti. Nessun costo, e i prezzi
   possono essere piu' alti per compensare la loro commissione.

Quello che **non** fare: comprare follower, fare aste a 5 EUR (margine zero e
cliente sbagliato), o aprire Etsy pagando la listing fee prima di aver venduto
il primo pezzo qui.

## Limiti dichiarati

- **L'API non pubblica nello store**: cinque inserimenti manuali sono
  inevitabili. Ho scritto il documento con i valori esatti perche' il lavoro sia
  di 15 minuti e non di un pomeriggio.
- **Le campagne richiedono URL pubblici**: finche' le immagini non sono online
  le campagne non si creano (il dry-run lo dice con `[ATTESA URL]`, non fallisce
  a meta').
- **Costi**: il costo Hoplix e' documentato a 7,90 IVA incl. (catalogo API 7,50).
  I margini qui usano **7,90**, cioe' il valore peggiore: se il catalogo vero
  dice 7,50 il margine e' migliore, mai peggiore. Verificalo con `--check`.
- **Printify resta l'alternativa** se servira' un catalogo molto piu' ampio o
  se Hoplix dovesse bloccare il negozio: il codice c'e' (`publish.py`), e' solo
  in pausa.
- **Nessun prodotto e' stato pubblicato e nessun euro e' stato speso** durante
  questo lavoro: servono le credenziali Hoplix e la decisione di aprire lo
  store.

## Comandi utili

```bash
../venv/bin/python platforms.py --price 24.99    # confronto e margini
../venv/bin/python catalog.py                    # tabella delle 5 schede
../venv/bin/python catalog.py --write-store --write-webapp
../venv/bin/python publish_hoplix.py             # dry-run campagne
../venv/bin/python publish_hoplix.py --check     # catalogo vero + margine
../venv/bin/python hoplix.py --catalog           # prodotti e costi base
../venv/bin/python -m pytest -q                  # test (offline)
```
