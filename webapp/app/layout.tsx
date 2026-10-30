import type { Metadata, Viewport } from 'next'
import './globals.css'
import Nav from '../components/Nav'

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://quotaverace.vercel.app'

/**
 * Layout RADICE come Server Component.
 *
 * Prima era `'use client'`: in App Router un layout client NON puo' esportare
 * `metadata`, quindi ogni pagina arrivava al browser senza titolo, senza
 * descrizione e senza immagine di anteprima (condivisione link e risultati di
 * ricerca vuoti). Il pezzo interattivo (navigazione, stato attivo) vive in
 * `components/Nav.tsx`.
 */
export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: 'QuotaVerace — value betting con rischio misurato',
    template: '%s · QuotaVerace',
  },
  description:
    'Dashboard di un sistema automatico di value betting: segnali confrontati con il mercato devigato (Pinnacle, metodo Shin), CLV misurato alla chiusura, staking frazionato e circuit breaker. Numeri reali, perdite incluse.',
  applicationName: 'QuotaVerace',
  keywords: [
    'value betting',
    'CLV',
    'closing line value',
    'quote devigate',
    'staking Kelly',
    'bankroll management',
  ],
  openGraph: {
    type: 'website',
    locale: 'it_IT',
    url: SITE_URL,
    siteName: 'QuotaVerace',
    title: 'QuotaVerace — value betting con rischio misurato',
    description:
      'Segnali confrontati col mercato devigato, CLV misurato sulla chiusura e risk management. Numeri verificabili, perdite incluse.',
  },
  robots: { index: true, follow: true },
}

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#111827',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body className="min-h-screen bg-gray-900 text-white">
        <Nav />
        {children}
      </body>
    </html>
  )
}
