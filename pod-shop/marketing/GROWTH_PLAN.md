# Piano di crescita — da 5 a 30 prodotti, con 2-4 ordini a settimana

Il piano parte da un vincolo reale: **cinque prodotti e nessun traffico**. Non
esiste una scorciatoia legale che porti ordini senza traffico, quindi la
sequenza è: mettere online → far arrivare persone → leggere i numeri → ampliare
solo dove c'è segnale.

Nessun numero in questo documento è un risultato ottenuto. Sono traguardi da
verificare, e vanno riportati anche quando non si raggiungono.

## 1. La matematica da cui parte tutto

Con prezzo 24,99 € e margine 12,58 € per pezzo:

| ordini/settimana | ricavo lordo | margine |
|---|---|---|
| 1 | 24,99 € | 12,58 € |
| 2 | 49,98 € | 25,16 € |
| 4 | 99,96 € | 50,32 € |
| 10 | 249,90 € | 125,80 € |

Per fare 2-4 ordini con una conversione realistica dell'1-2% (standard per
questo tipo di negozio) servono **100-400 sessioni a settimana** sulla vetrina.
Questa è la ragione per cui il piano mette Pinterest e i contenuti SEO prima di
qualunque altra cosa: sono gli unici canali che portano 100+ sessioni a costo
zero.

## 2. Quattro fasi (8 settimane)

### Fase 1 — Settimane 1-2: esistere
- [x] 5 design pronti, testi e prezzi calcolati
- [x] vetrina del sito con immagini ottimizzate (WebP 16-19 KB)
- [ ] negozio Hoplix attivo (Passi 1-3 di `GO_LIVE.md`)
- [ ] 5 schede inserite dal documento pronto
- [ ] 5 campagne create (`publish_hoplix.py --live`) → 5 link
- [ ] `NEXT_PUBLIC_SHOP_URL` impostata, pulsante "Acquista" attivo
- [ ] 5 pin Pinterest, 3 board, bio
- [ ] 5 video TikTok girati (script in `social.md`)

Misura a fine fase: il negozio si apre, il link funziona, i pin sono online.
**Zero vendite in questa fase è normale**, il canale non esiste ancora.

### Fase 2 — Settimane 3-4: farsi trovare
- [ ] 3 articoli SEO pubblicati (i primi tre di `SEO.md` §5)
- [ ] 1 pin nuovo a settimana (stesso design, immagine diversa)
- [ ] 3 video/settimana
- [ ] Search Console collegata, prime query osservate

Misura a fine fase: sessioni/settimana, clic in uscita verso il negozio, pin
salvati. **Ancora nessuna conclusione**: con 2 settimane di dati non si decide
nulla.

### Fase 3 — Settimane 5-6: ampliare dove c'è segnale
Ordine di aggiunta, in base a cosa si è mosso (non in base ai gusti):
1. **Varianti dei design che hanno cliccato** (es. il 404 genera clic → esce
   "404 — amico non trovato"): riuso del template, un'ora di lavoro.
2. **Un prodotto non-maglietta** (adesivo o tazza) sul design più venduto:
   prezzo d'ingresso basso, alza il numero di primi acquisti.
3. **I 5 design nuovi** con priorità 1 di `ideas.csv`: errore 500, riavvia,
   segretaria santa, // TODO: regalo.

Target a fine fase: **10-12 prodotti online**.

### Fase 4 — Settimane 7-8: 20-30 prodotti e selezione
Il catalogo si allarga **solo** in questa direzione: un design che ha mostrato
clic e zero ordini ha un problema di prezzo o di pagina (non di grafica), e
duplicarlo cinque volte moltiplica il problema.

Per arrivare a 20-30:
- 8-10 design nuovi (priorità 2 e 3 di `ideas.csv`), più varianti dei migliori;
- **cross-listing** su Redbubble/Teepublic con i **file già pronti** in
  `redbubble-tech-gift-2026-work/output/upload/design/` — nessun costo, prezzi
  più alti per compensare la loro commissione;
- **ritiro dei design che dopo 4 settimane** hanno zero clic con almeno 50
  impressioni: un catalogo pieno di prodotti morti diluisce l'attenzione.

## 3. Cosa si misura, ogni lunedì (10 minuti)

| metrica | fonte | soglia di allarme | cosa fare se scatta |
|---|---|---|---|
| sessioni `/shop` | analytics sito | < 20/sett dopo 3 settimane | il problema è il traffico: più pin/articoli, non toccare il prezzo |
| clic in uscita verso il negozio | analytics | < 5% delle sessioni | la pagina non convince: foto/mockup, non prezzo |
| ordini | pannello Hoplix | 0 con > 100 sessioni in 2 settimane | il problema è prezzo/fiducia: rivedere spedizione e recensioni |
| margine medio per ordine | pannello Hoplix | < 8 € | controllare che il costo base non sia salito |
| resi / rimborsi | pannello Hoplix | > 1 su 10 | problema di taglie o stampa: aggiornare la guida taglie |

**Visite, clic, salvataggi e visualizzazioni non sono vendite.** Vanno riportati
come metriche separate, sempre.

## 4. Prezzo: quando si tocca e quando no

- **Non scendere sotto 19,99 €**: il margine passa a 8,49 € e ogni reso brucia
  tre vendite.
- **Alzare** solo se le vendite arrivano e i resi restano bassi: 27,99 € porta
  il margine a 15,04 € e il rischio è perdere volume, non margine.
- **Le promozioni "2 magliette"** sono preferibili allo sconto secco: alzano il
  valore medio senza distruggere il margine unitario.

## 5. Cosa costerebbe soldi (e quindi non si fa senza un tuo sì)

| voce | costo indicativo | perché non ora |
|---|---|---|
| dominio dedicato | 10-15 €/anno | `quotaverace.vercel.app` funziona e non costa |
| annunci Instagram/TikTok | 50-200 €/mese | senza conversione misurata si compra traffico che non compra |
| Etsy (listing fee) | 0,20 €/listing + commissioni | ha senso dopo aver dimostrato che il design vende |
| tool SEO a pagamento | 20-100 €/mese | Search Console è gratis |

## 6. Criterio di verità

Il progetto non dichiara un successo finché non c'è un ordine pagato nel
pannello. Un pin con 500 salvataggi è un'ipotesi; un ordine è un fatto.

Se dopo 8 settimane gli ordini sono zero con 300+ sessioni, la conclusione
onesta non è "serve più traffico": è che l'offerta (prezzo, design, tempi) non
converte, e va cambiata prima di continuare.
