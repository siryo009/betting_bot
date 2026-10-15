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
| **GATE** | puo' RIFIUTARE (o solo restringere) un candidato | non puo' promuovere u