'use client'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

const LINKS = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/agenti', label: '🏛️ Agenti' },
  { href: '/cassa', label: '💰 Cassa' },
  { href: '/calendario', label: 'Calendario' },
  { href: '/value', label: 'Value' },
  { href: '/movimenti', label: 'Movimenti' },
  { href: '/calibrazione', label: '🎯 Calibrazione' },
  { href: '/schedina', label: 'Schedina' },
  { href: '/calcola', label: 'Calcola' },
  { href: '/backtest', label: 'Backtest' },
  { href: '/storico', label: 'Storico' },
  // La voce compare solo se il negozio esiste: un link a una pagina vuota
  // e' peggio di nessun link. `NEXT_PUBLIC_*` e' inlineata a build time.
  ...(process.env.NEXT_PUBLIC_SHOP_URL ? [{ href: '/shop', label: '🛒 Shop' }] : []),
]

/**
 * Barra di navigazione.
 *
 * Perche' e' un componente CLIENT separato dal layout: il layout deve restare
 * un Server Component per poter esportare `metadata` (titolo, descrizione,
 * Open Graph). Prima il layout era interamente `'use client'`, quindi il sito
 * non aveva NESSUN titolo ne' descrizione nelle pagine.
 *
 * `next/link` invece di `<a>`: con `<a>` ogni clic ricaricava tutta l'applicazione
 * (HTTP completo, stato perso, ~100 kB di JS riscaricati), con `Link` la
 * navigazione e' lato client.
 */
export default function Nav() {
  const pathname = usePathname() || ''
  return (
    <nav
      aria-label="Navigazione principale"
      className="sticky top-0 z-40 border-b border-gray-700 bg-gray-900/95 backdrop-blur px-4 py-3"
    >
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2">
        <Link
          href="/dashboard"
          className="mr-2 shrink-0 text-lg font-bold text-emerald-400 sm:text-xl"
        >
          ⚽ QuotaVerace
        </Link>
        {LINKS.map((l) => {
          const active =
            pathname === l.href ||
            (l.href !== '/dashboard' && pathname.startsWith(l.href + '/'))
          return (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active ? 'page' : undefined}
              className={`whitespace-nowrap text-sm transition sm:text-base ${
                active
                  ? 'border-b-2 border-emerald-400 font-medium text-emerald-300'
                  : 'hover:text-emerald-400'
              }`}
            >
              {l.label}
            </Link>
          )
        })}
      </div>
    </nav>
  )
}
