'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO = [
  { evento: 'Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, ev: 0.155, risultato: 'pending', profit: null },
  { evento: 'Juventus vs Atalanta', esito: '1', quota: 2.20, ev: 0.056, risultato: 'won', profit: 1.20 },
  { evento: 'Roma vs Milan', esito: 'X', quota: 3.20, ev: -0.04, risultato: 'lost', profit: -1.0 },
]

export default function Storico() {
  const [signals, setSignals] = useState<any[]>(DEMO)
  const [summary, setSummary] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/storico`)
        if (!res.ok) throw new Error('fallback demo')
        const j = await res.json()
        if (j.segnali?.length) { setSignals(j.segnali); setSummary(j.summary) }
      } catch { setUsingDemo(true) }
    })()
  }, [])

  const stats = summary || { closed: 24, won: 14, lost: 10, roi: 8.3 }

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold">📜 Storico Segnali</h1>
        <Link href="/schedina" className="text-sm bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg font-medium transition">
          📋 Schedina
        </Link>
      </div>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}
      {/* overflow-x-auto: su mobile la tabella scorre invece di essere tagliata
          (con overflow-hidden le ultime colonne - Profitto - sparivano) */}
      <div className="bg-gray-800 rounded-xl overflow-x-auto">
        <table className="w-full min-w-[640px] text-left">
          <thead className="bg-gray-700"><tr><th className="p-4">Evento</th><th className="p-4">Esito</th><th className="p-4">Quota</th><th className="p-4">EV</th><th className="p-4">Risultato</th><th className="p-4">Profitto</th></tr></thead>
          <tbody>
            {signals.map((s: any, i: number) => {
              const evPct = Number(s.ev) * 100
              const won = s.risultato === 'won'
              const lost = s.risultato === 'lost'
              return (
                <tr key={i} className="border-b border-gray-700 hover:bg-gray-700/50 transition">
                  <td className="p-4">{s.evento}</td><td className="p-4">{s.esito}</td><td className="p-4">{Number(s.quota).toFixed(2)}</td>
                  <td className={`p-4 font-bold ${evPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{evPct.toFixed(1)}%</td>
                  <td className="p-4 text-lg">{won ? '✅' : lost ? '❌' : '⏳'}</td>
                  <td className="p-4">{s.profit != null ? `${Number(s.profit) >= 0 ? '+' : ''}${Number(s.profit).toFixed(2)}u` : '-'}</td>
                </tr>
              )
            })}
            {signals.length === 0 && <tr><td className="p-4 text-gray-500" colSpan={6}>Nessun segnale.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="mt-6 bg-gray-800 rounded-xl p-6">
        <h2 className="text-xl font-bold mb-4">📈 Riepilogo 30 giorni</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Segnali chiusi" value={String(stats.closed ?? 0)} />
          <Stat label="Vinti" value={String(stats.won ?? 0)} />
          <Stat label="Persi" value={String(stats.lost ?? 0)} />
          <Stat label="ROI" value={`${Number(stats.roi ?? 0).toFixed(1)}%`} highlight={Number(stats.roi ?? 0) >= 0} />
        </div>
      </div>
    </main>
  )
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return <div className={`p-4 rounded-lg text-center ${highlight ? 'bg-emerald-900/30 border border-emerald-500/30' : 'bg-gray-700'}`}><div className="text-gray-400 text-sm">{label}</div><div className={`text-2xl font-bold ${highlight ? 'text-emerald-400' : ''}`}>{value}</div></div>
}