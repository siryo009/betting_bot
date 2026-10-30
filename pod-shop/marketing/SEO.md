# Strategia SEO — negozio regali tech (Natale 2026)

Obiettivo dichiarato: **traffico organico gratuito che compra**, non posizioni
su keyword che non portano visite. Nessun numero in questo documento è un
risultato: sono obiettivi e ipotesi di lavoro, da verificare con Search Console.

## 1. Chi cerca cosa (tre intenzioni, tre pagine)

| intenzione | chi è | query tipiche | pagina che deve rispondere |
|---|---|---|---|
| **regalo per qualcun altro** | chi cerca un regalo di Natale per un amico/collega che lavora al computer | "regalo per programmatore", "regalo informatico natale", "regalo per amico nerd" | home + `/shop` + guida regali |
| **regalo per collega / segretaria santa** | chi partecipa a un Secret Santa in ufficio e cerca qualcosa di economico e divertente | "regalo segretaria santa collega", "regalo ufficio sotto 30 euro" | pagina guida "regalo per collega" |
| **auto-acquisto / appassionato** | chi compra per sé una maglietta con una battuta tech | "maglietta programmatore", "t-shirt informatica divertente" | schede prodotto |

La regola che evita il lavoro inutile: **una pagina per intenzione**, non una
pagina per keyword. Chi cerca un regalo e chi cerca una maglietta sono due
persone diverse, e una pagina sola non può parlare bene a entrambe.

## 2. Mappa keyword → pagina (partenza concreta)

Cluster A — **regalo per programmatore** (competizione media, intento
commerciale alto):
`regalo per programmatore`, `regalo sviluppatore`, `regalo per chi programma`,
`regalo informatico natale`, `regalo tecnologico divertente`.

Cluster B — **maglietta tech** (competizione bassa, intento di acquisto):
`maglietta programmatore`, `t-shirt informatica divertente`, `maglietta con
battute da informatico`, `maglietta regalo nerd`.

Cluster C — **meta stagionale** (competizione alta, ma il traffico arriva
comunque a chi è già in pagina):
`regalo natale 2026 informatico`, `regali di natale per chi lavora al pc`,
`idee regalo low cost natale`.

**Assegnazione:**

| pagina | H1 | title (≤ 60 char) | meta description (≤ 155) |
|---|---|---|---|
| `/shop` | Regali tech per chi vive davanti a un computer | Magliette e regali per programmatori \| QuotaVerace Shop | Cinque magliette disegnate da zero, battute da informatico, 24,99 €. Spedizione dall'Europa, regalo pronto. |
| guida (da creare) | Regali di Natale per programmatori: 12 idee sotto 30 € | Regali per programmatori 2026: idee sotto 30 € | Idee regalo per chi programma: magliette, adesivi e piccoli oggetti, senza spendere più di 30 €. |
| scheda × 5 | *nome del design* | *battuta* — maglietta da programmatore \| 24,99 € | *una riga sul design* — cotone, taglie S-2XL, spedito dall'Europa. |

Le schede prodotto del sito **non esistono ancora come URL dedicate**: oggi la
vetrina è un'unica pagina con le card. Il passo con il rapporto
costo/beneficio migliore è generare una pagina per design (`/shop/[slug]`):
cinque URL indicizzabili con testo unico valgono più di dieci articoli di blog.

## 3. Cosa mettere in pagina (on-page)

- **Un testo unico per design** (già presente in `LISTINGS.csv`: descrizione
  lunga, struttura "PER CHI È / DETTAGLI / SPEDIZIONE"). È il materiale che
  distingue la pagina dall'ennesimo elenco di immagini.
- **Alt text descrittivo e onesto**: `Maglietta bianca con la scritta CTRL + ALT
  + REGALA` — non keyword stuffing, che oggi penalizza.
- **Dati strutturati** (JSON-LD) su `/shop`: `Product` con `name`,
  `description`, `image`, `offers` (`price: 24.99`, `priceCurrency: EUR`,
  `availability`), `brand`. Con cinque prodotti si scrive in mezz'ora e rende
  le schede eleggibili per i rich result.
- **Un solo H1 per pagina**, H2 per sezione, link interni tra guida e schede.
- **Nessuna recensione inventata**: le `AggregateRating` finte sono la via più
  rapida per una penalizzazione. Si aggiungono quando ci sono recensioni vere.

## 4. Tecnico (stato attuale, verificato con la build)

| voce | stato | nota |
|---|---|---|
| immagini | **ok** | card WebP da 16-19 KB invece dei PNG da ~250 KB: la pagina `/shop` passa da ~1,4 MB a ~85 KB |
| peso JS pagina shop | **ok** | 87,5 kB First Load (Next.js statico) |
| viewport / mobile | **ok** | Tailwind, griglia 1 colonna su telefono, `next/image` |
| title + description | **ok** | `metadata` in `app/shop/page.tsx` |
| URL prodotto | **da fare** | una pagina per design (`/shop/[slug]`) |
| sitemap.xml + robots.txt | **da fare** | Next li genera da `app/sitemap.ts` (10 righe) |
| canonical | **da fare** | evita duplicati tra `/shop` e pagine prodotto |
| Open Graph image | **da fare** | un'immagine 1200×630 per la condivisione: la stessa si riusa sui social |
| Search Console | **tuo, 3 minuti** | proprietà `quotaverace.vercel.app`, verifica con tag HTML |

## 5. Contenuti: 8 pezzi, in ordine di resa attesa

Scritti per rispondere a una domanda, non per "fare SEO". Ognuno linka a 2-3
schede prodotto.

1. **Regali di Natale per programmatori: 12 idee sotto 30 €** (pilastro, punta
   al Cluster A).
2. **Cosa regalare a un collega che lavora in IT (senza spendere 100 €)**.
3. **30 battute da maglietta per informatici** (contenuto che si condivide da
   solo, porta link).
4. **Il regalo di Natale per chi risponde "hai provato a riavviare?"**.
5. **Segretaria santa in ufficio: idee sotto 20 €** (Cluster B/C, molto
   stagionale).
6. **CTRL + ALT + REGALA: come è nato un design** (storia = testo unico, e i
   motori valutano i testi unici).
7. **Magliette per sviluppatori: guida a taglie e vestibilità** (riduce i resi,
   che sono il vero costo nascosto).
8. **Regalo last minute: cosa arriva prima di Natale** (cattura chi cerca il
   20 dicembre; va aggiornato con le date reali di spedizione).

Cadenza realistica: **1 pezzo a settimana**, non cinque in un giorno. Un
articolo pubblicato e poi abbandonato non porta traffico; cinque pubblicati lo
stesso giorno si fanno concorrenza tra loro.

## 6. Come si misura (e cosa NON è un risultato)

Da leggere ogni lunedì, in Search Console + analytics:

| metrica | cosa dice | cosa non dice |
|---|---|---|
| impressioni | quante volte siamo comparsi | che qualcuno ha cliccato |
| CTR | se il title funziona | se il prodotto piace |
| posizione media | dove siamo per una query | che la query porti soldi |
| clic | traffico reale | **vendite** |
| sessioni `/shop` | interesse per la vetrina | conversione |

**Visite ≠ vendite.** Il numero che conta è in fondo alla filiera: ordini nel
pannello Hoplix. Un picco di 200 visite e zero ordini è un'informazione utile
(il prezzo o la pagina non convincono), non un successo.

Obiettivi dichiarati, da verificare e non da annunciare: nelle prime 4 settimane
**20-50 sessioni/settimana** dalle guide e dai social, con **2-4 ordini a
settimana** come traguardo. Se dopo 4 settimane le sessioni ci sono e gli ordini
no, il problema è la pagina o il prezzo, non il traffico.

## 7. Cose che non facciamo

- Nessun testo generato in serie con la keyword ripetuta: si legge male e si
  posiziona peggio.
- Nessun link comprato, nessuna rete di blog.
- Nessun dato inventato nei testi ("oltre 1000 clienti soddisfatti" prima del
  primo cliente).
