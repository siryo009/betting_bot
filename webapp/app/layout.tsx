'use client'
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
]

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || ''
  return (
    <html lang="it">
      <body className="bg-gray-900 text-white min-h-screen">
        <nav className="p-4 border-b border-gray-700 flex gap-5 items-center overflow-x-auto">
          <span className="font-bold text-emerald-400 text-xl">⚽ QuotaVerace</span>
          {LINKS.map(l => {
            const active = pathname === l.href || (l.href !== '/dashboard' && pathname.startsWith(l.href + '/'))
            return (
              <a key={l.href} href={l.href}
                className={`transition whitespace-nowrap ${active ? 'text-emerald-300 font-medium border-b-2 border-emerald-400' : 'hover:text-emerald-400'}`}>
                {l.label}
              </a>
            )
          })}
        </nav>
        {children}
      </body>
    </html>
  )
}