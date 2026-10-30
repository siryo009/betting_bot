/**
 * Indirizzo del negozio, in un posto solo e VALIDATO.
 *
 * Perche' un modulo e non `process.env` sparso nelle pagine: lo stesso valore
 * decide due cose diverse — la voce "Shop" in navigazione (`Nav.tsx`) e la
 * destinazione dei pulsanti "Acquista" (`app/shop/page.tsx`) — e se le due
 * letture divergessero si avrebbe un menu che porta a una pagina di prodotti
 * senza modo di comprarli (o il contrario).
 *
 * `NEXT_PUBLIC_*` e' **inlineata a build time**: cambiare l'indirizzo richiede
 * un redeploy, non basta salvarlo su Vercel.
 *
 * Cosa succede se il valore manca o e' scritto male: si comporta come assente.
 * Un `NEXT_PUBLIC_SHOP_URL="quotaverace.hoplix.shop"` (senza schema) produrrebbe
 * un link relativo che non porta da nessuna parte: meglio un pulsante "In
 * arrivo" di un pulsante che sembra attivo e non lo e'.
 */

const RAW = (process.env.NEXT_PUBLIC_SHOP_URL || '').trim()

export function shopUrl(): string {
  if (!RAW) return ''
  try {
    const url = new URL(RAW)
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return ''
    return url.toString().replace(/\/$/, '')
  } catch {
    return ''
  }
}

/** true solo se il negozio e' configurato (e leggibile): governa la voce di menu. */
export const shopLinked = shopUrl() !== ''
