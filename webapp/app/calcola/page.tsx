'use client'
import { useState } from 'react'
export default function Calcola() {
  const [home, setHome] = useState('Inter')
  const [away, setAway] = useState('Napoli')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState<string | null>(null)

  // Prima non c'era nessuna gestione degli errori: con il backend giu' o con una
  // risposta non-JSON la pagina restava a schermo vuoto (e `loading` non tornava
  // mai a false su un throw, lasciando il bottone disabilitato per sempre).
  async function calc() {
    if (!home.trim() || !away.trim()) {
      setError('Inserisci il nome di entrambe le squadre.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(
        `/api/poisson?home=${encodeURIComponent(home.trim())}&away=${encodeURIComponent(away.trim())}`
      )
      const j = await res.json().catch(() => null)
      if (!res.ok) {
        throw new Error(j?.detail || j?.error || `Il backend ha risposto ${res.status}.`)
      }
      if (!j || typeof j.prob_1 !== 'number') {
        throw new Error('Risposta del backend non valida (probabilita' + String.fromCharCode(39) + ' assenti).')
      }
      setResult(j)
    } catch (e) {
      setResult(null)
      setError(
        e instanceof Error && e.message
          ? e.message
          : 'Backend non raggiungibile: riprova tra poco.'
      )
    } finally {
      setLoading(false)
    }
  }
  return (
    <main className="p-6 max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">⚽ Calcolatore Poisson</h1>
      <div className="flex gap-3 mb-6">
        <div className="flex-1"><label className="block text-gray-400 text-sm mb-1">Casa</label><input value={home} onChange={e => setHome(e.target.value)} className="w-full p-3 rounded-lg bg-gray-800 border border-gray-600 focus:border-emerald-500 outline-none" /></div>
        <div className="flex-1"><label className="block text-gray-400 text-sm mb-1">Trasferta</label><input value={away} onChange={e => setAway(e.target.value)} className="w-full p-3 rounded-lg bg-gray-800 border border-gray-600 focus:border-emerald-500 outline-none" /></div>
        <div className="flex items-end"><button onClick={calc} disabled={loading || !home.trim() || !away.trim()} className="px-6 py-3 bg-emerald-600 hover:bg-emerald-500 rounded-lg font-bold transition disabled:opacity-50">{loading ? '...' : 'Calcola'}</button></div>
      </div>
      {error && (
        <div role="alert" className="mb-6 rounded-xl border border-red-500/40 bg-red-900/30 p-4 text-sm text-red-200">
          ⚠️ {error}
        </div>
      )}
      {result && (
        <div className="bg-gray-800 rounded-xl p-6 space-y-4">
          <h2 className="text-xl font-bold">{result.home} vs {result.away}</h2>
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-gray-700 p-4 rounded-lg"><div className="text-gray-400 text-sm">λ Casa</div><div className="text-2xl font-bold">{result.lambda_home}</div></div>
            <div className="bg-gray-700 p-4 rounded-lg"><div className="text-gray-400 text-sm">λ Trasferta</div><div className="text-2xl font-bold">{result.lambda_away}</div></div>
          </div>
          <div className="grid grid-cols-3 gap-3"><ProbBox label="1" value={result.prob_1} /><ProbBox label="X" value={result.prob_X} /><ProbBox label="2" value={result.prob_2} /></div>
          <div className="grid grid-cols-2 gap-3"><ProbBox label="Over 2.5" value={result.over25} /><ProbBox label="Under 2.5" value={result.under25} /></div>
          <div className="bg-emerald-900/30 border border-emerald-500/30 p-4 rounded-lg">
            <div className="text-emerald-400 font-bold">🎯 Miglior segnale: {result.ev_best}</div>
            <div className="text-sm">Stake Kelly: {result.kelly_stake}</div>
          </div>
        </div>
      )}
    </main>
  )
}
function ProbBox({ label, value }: { label: string; value: number }) {
  // un valore assente non deve diventare "NaN%": si dichiara "—"
  const pct = Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : '—'
  return <div className="bg-gray-700 p-3 rounded-lg text-center"><div className="text-gray-400 text-sm">{label}</div><div className="text-xl font-bold">{pct}</div></div>
}
