import type { Metadata } from 'next'
import { SHOP_CURRENCY, SHOP_PRODUCTS } from '../../content/shop'

/**
 * Vetrina dello shop.
 *
 * PERCHE' UNA PAGINA QUI E NON SOLO IL NEGOZIO HOPLIX: il sito e' l'unico posto
 * che possiamo linkare senza dipendere dall'algoritmo di nessuno, e le schede
 * dello store hanno bisogno di un URL da cui raggiungerle. Questa pagina non
 * vende: mostra e porta al negozio, dove il pagamento lo gestisce Hoplix
 * (incasso e fattura a carico loro, non nostro).
 *
 * Server Component: nessun JS spedito al browser per mostrare delle card.
 * Il manifest e' generato da `pod-shop/catalog.py` — non si scrive a mano,
 * altrimenti prezzi e immagini divergono dal catalogo vero.
 */
export const metadata: Metadata = {
  title: 'Shop — regali per chi vive davanti a uno schermo',
  description:
    'Magliette con stampe originali per programmatori, smanettoni e regali dell ultimo minuto. Produzione in Europa, spedizione tracciata, pagamento sicuro.',
  openGraph: {
    title: 'Shop — regali per chi vive davanti a uno schermo',
    description: 'Cinque stampe originali per chi passa la giornata tra tastiera e scadenze. 24,99 EUR, produzione europea.',
    type: 'website',
    locale: 'it_IT',
  },
}

const SHOP_URL = (process.env.NEXT_PUBLIC_SHOP_URL || '').trim()

function price(value: number): string {
  return new Intl.NumberFormat('it-IT', { style: 'currency', currency: SHOP_CURRENCY }).format(value)
}

const STEPS = [
  { title: 'Scegli la maglietta', text: 'Cinque stampe originali, taglie dalla S alla 2XL.' },
  { title: 'Paghi sul negozio', text: 'Il pagamento e la fattura sono gestiti dal laboratorio: nessun dato della carta passa da qui.' },
  { title: 'Produzione in 2-5 giorni', text: 'Stampata su richiesta in un laboratorio europeo, poi spedita con codice di tracciamento.' },
]

export default function ShopPage() {
  const products = SHOP_PRODUCTS

  // Dati strutturati: prezzo e disponibilita' VERI dal manifest, nessuna
  // recensione inventata (una AggregateRating finta e' la via piu' rapida a
  // una penalizzazione, e prima del primo cliente non esiste).
  const jsonLd = {
    '@context': 'https://schema.org',
    '@type': 'ItemList',
    itemListElement: products.map((p, index) => ({
      '@type': 'ListItem',
      position: index + 1,
      item: {
        '@type': 'Product',
        name: p.name,
        image: p.image,
        description: `Maglietta con stampa originale «${p.name}». Produzione europea, spedizione tracciata.`,
        brand: { '@type': 'Brand', name: 'QuotaVerace Shop' },
        offers: {
          '@type': 'Offer',
          price: p.price,
          priceCurrency: SHOP_CURRENCY,
          availability: 'https://schema.org/InStock',
          ...(p.url || SHOP_URL ? { url: p.url || SHOP_URL } : {}),
        },
      },
    })),
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <header className="mb-8">
        <h1 className="text-2xl font-bold text-emerald-400 sm:text-3xl">Shop</h1>
        <p className="mt-2 max-w-2xl text-sm text-gray-300 sm:text-base">
          Stampe originali per chi passa la giornata tra tastiera e scadenze. Nessun logo,
          nessuna interfaccia copiata: i disegni sono fatti da zero.
        </p>
        <p className="mt-2 text-xs text-gray-400">
          Produzione europea · spedizione tracciata · reso se il capo arriva difettoso.
        </p>
      </header>

      {products.length === 0 ? (
        <p className="rounded-lg border border-gray-700 bg-gray-800 p-6 text-sm text-gray-300">
          Catalogo in preparazione: le schede compaiono qui appena pubblicate.
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {products.map((p) => {
            const href = p.url || SHOP_URL
            return (
              <li key={p.id} className="flex flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-800">
                <div className="flex items-center justify-center bg-white p-3">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={p.image}
                    alt={`Maglietta con la stampa «${p.name}»`}
                    width={p.imageW}
                    height={p.imageH}
                    loading="lazy"
                    className="h-64 w-auto object-contain"
                  />
                </div>
                <div className="flex flex-1 flex-col gap-3 p-4">
                  <div>
                    <h2 className="text-base font-semibold text-white">{p.name}</h2>
                    <p className="mt-1 text-xs text-gray-400">
                      Taglie {p.sizes.join(' · ')} · capo {p.colors.includes('black') ? 'scuro' : 'chiaro'}
                    </p>
                  </div>
                  <div className="mt-auto flex items-center justify-between gap-3">
                    <span className="text-lg font-bold text-emerald-400">{price(p.price)}</span>
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-gray-900 hover:bg-emerald-400"
                      >
                        Acquista
                      </a>
                    ) : (
                      <span className="text-xs text-gray-400">In arrivo</span>
                    )}
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      <section className="mt-10 grid grid-cols-1 gap-4 sm:grid-cols-3">
        {STEPS.map((s) => (
          <div key={s.title} className="rounded-xl border border-gray-700 bg-gray-800 p-4">
            <h3 className="text-sm font-semibold text-emerald-300">{s.title}</h3>
            <p className="mt-1 text-xs text-gray-300">{s.text}</p>
          </div>
        ))}
      </section>

      <section className="mt-8 rounded-xl border border-gray-700 bg-gray-800 p-5 text-sm text-gray-300">
        <h2 className="text-base font-semibold text-white">Spedizione e resi</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
          <li>Italia: 4,95 EUR, consegna in 1-3 giorni dopo la produzione.</li>
          <li>Europa: spedizione calcolata dal negozio al momento dell ordine.</li>
          <li>Capo difettoso o stampa sbagliata: ristampa o rimborso, senza restituire il capo.</li>
          <li>Il pagamento avviene sul negozio; questa pagina non raccoglie dati di pagamento.</li>
        </ul>
      </section>
    </main>
  )
}
