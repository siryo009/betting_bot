'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

export default function Calendario() {
  const [data, setData] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  async function load() {
    try {
      const res = await fetch(`${API_BASE}/api/calendario`)
      if (!res.ok) throw new Error('fallback')
      setData(await res.json())
    } catch { setUsingDemo(true); setData({ partite: [], n: 0 }) }
  }

  async function refreshAnalysis() {
    setRefreshing(true)
    try {
      await fetch(`${API_BASE}/api/analisi`, { method: 'POST' })
      await load()
    } catch { /* ignora */ }
    setRefreshing(false)
  }

  useEffect(() => { load() }, [])

  const partite: any[] = data?.partite || []
  const oggi = new Date().toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' })

  // raggruppa per campionato
  const byLeague: Record<string, any[]> = {}
  for (const p of partite) (byLeague[p.league] = byLeague[p.league] || []).push(p)

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📅 Calendario</h1>
        <div className="flex gap-3">
          <button onClick={refreshAnalysis} disabled={refreshing}
            className="text-sm bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 px-4 py-2 rounded-lg font-medium transition">
            {refreshing ? '⏳ Aggiorno...' : '🔄 Aggiorna analisi'}
          </button>
          <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
        </div>
      </div>
      <p className="text-gray-400 text-sm mb-6 capitalize">🗓 {oggi} · {partite.length} partite con analisi</p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — nessun dato. Prova /analisi nel bot.</p>}

      {partite.length === 0 && !usingDemo && (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-400">
          Nessuna partita oggi con analisi.<br />
          <span className="text-sm">Esegui <code className="text-blue-400">/analisi</code> nel bot per aggiornare calendario e quote.</span>
        </div>
      )}

      {Object.entries(byLeague).map(([league, matches]) => (
        <div key={league} className="mb-8">
          <h2 className="text-xl font-bold mb-3">🏆 {league}</h2>
          {/* su mobile le 6 colonne non entrano: si scorre, non si taglia */}
          <div className="bg-gray-800 rounded-xl overflow-x-auto">
            <table className="w-full min-w-[640px] text-left">
              <thead className="bg-gray-700">
                <tr><th className="p-3">Ora</th><th className="p-3">Partita</th><th className="p-3">Segnale</th><th className="p-3">Quota</th><th className="p-3">EV</th><th className="p-3">Mercato</th></tr>
              </thead>
              <tbody>
                {matches.map((p: any) => {
                  const time = p.commence?.slice(11, 16) || '--:--'
                  const st = p.analisi_status
                  const ev = p.best_ev != null ? Number(p.best_ev) * 100 : null
                  const edge = p.market_edge != null ? Number(p.market_edge) * 100 : null
                  const badge = st === 'strong_value' ? '🔥' : st === 'value' ? '🟡' : st === 'rejected' ? '❌' : '⚪'
                  return (
                    <tr key={p.match_id || `${p.home}-${p.away}`} className="border-b border-gray-700 hover:bg-gray-700/50">
                      <td className="p-3 text-gray-400">{time}</td>
                      <td className="p-3 font-medium">{p.home} vs {p.away}</td>
                      <td className="p-3">{badge} {p.best_esito || '—'}</td>
                      <td className="p-3">{p.best_quota ? Number(p.best_quota).toFixed(2) : '—'}</td>
                      <td className={`p-3 font-bold ${ev != null && ev >= 0 ? 'text-emerald-400' : ev != null ? 'text-red-400' : 'text-gray-500'}`}>
                        {ev != null ? `${ev >= 0 ? '+' : ''}${ev.toFixed(1)}%` : '—'}
                      </td>
                      <td className="p-3 text-gray-400">{edge != null ? `${edge >= 0 ? '+' : ''}${edge.toFixed(1)}pp` : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </main>
  )
}
