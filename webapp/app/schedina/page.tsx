'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO = {
  picks: [
    { league: 'Serie A', home: 'Inter', away: 'Napoli', evento: 'Serie A – Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, bookmaker: 'Bet365', ev: 0.155, prob: 0.626, kelly_dynamic: 0.25, kelly_reason: 'EV alto · lega core' },
    { league: 'Liga Italia', home: 'Roma', away: 'Milan', evento: 'Liga Italia – Roma vs Milan', esito: '1', quota: 2.40, bookmaker: 'Sbobet', ev: 0.08, prob: 0.497, kelly_dynamic: 0.17, kelly_reason: 'EV contenuto · lega core' },
  ],
  multipla: {
    esiti: 'Over 2.5 + 1',
    quota: 5.04,
    prob: 0.311,
    ev: 0.568,
    legs: [
      { esito: 'Over 2.5', quota: 2.10, evento: 'Serie A – Inter vs Napoli' },
      { esito: '1', quota: 2.40, evento: 'Liga Italia – Roma vs Milan' },
    ],
  },
  bankroll: 100.0,
}

export default function Schedina() {
  const [data, setData] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/schedina`)
        if (!res.ok) throw new Error('fallback demo')
        const j = await res.json()
        if (!j.picks?.length && !j.multipla) {
          setData(j)
        } else {
          setData(j)
        }
      } catch {
        setData(DEMO)
        setUsingDemo(true)
      }
    })()
  }, [])

  const d = data || DEMO
  const picks: any[] = d.picks || []
  const multipla = d.multipla
  const bankroll = Number(d.bankroll || 100)

  // Stake adattivo dal backend (con fallback client-side)
  const getStake = (p: any) => {
    if (p.stake !== undefined && p.stake !== null) {
      return {
        stake: Number(p.stake),
        pct: (Number(p.stake) / bankroll) * 100,
        // Il KELLY mostrato e' quello del MOTORE (banda dinamica 0.15-0.25,
        // direttiva 04/10): se il backend non lo espone si ripiega sul
        // frazionamento del percorso storico per non lasciare il campo vuoto.
        kelly: Number(p.kelly_dynamic ?? p.stake_kelly ?? 0.25),
        reason: p.kelly_reason || p.stake_reason || '',
        dynamic: p.kelly_dynamic != null,
      }
    }
    // Fallback client-side: 1/4 Kelly cap 3%
    const prob = Number(p.prob)
    const quota = Number(p.quota)
    const kellyFull = (prob * quota - 1) / (quota - 1)
    const kelly = Math.max(0, kellyFull) / 4
    const cap = bankroll * 0.03
    const stake = Math.min(kelly * bankroll, cap)
    return { stake, pct: (stake / bankroll) * 100, kelly: 0.25, reason: 'fallback', dynamic: false }
  }

  const totalStake = picks.reduce((s, p) => s + getStake(p).stake, 0)

  return (
    <main className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📋 Schedina Hari Ini</h1>
        <div className="flex gap-3">
          <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
          <Link href="/storico" className="text-sm text-blue-400 hover:underline">Storico →</Link>
        </div>
      </div>
      <p className="text-gray-400 text-sm mb-6">💰 Bankroll referensi: <b className="text-emerald-400">€{bankroll.toFixed(2)}</b></p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}

      {picks.length === 0 ? (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-400">
          Belum ada partita dengan nilai positif hari ini.<br />
          <span className="text-sm">Jalankan <code className="text-blue-400">/analisi</code> di bot untuk memperbarui odds.</span>
        </div>
      ) : (
        <div className="space-y-4 mb-8">
          <h2 className="text-xl font-bold">🎯 Single Terbaik Hari Ini</h2>
          <p className="text-amber-400 text-xs">⚠️ Selalu mainkan single. Parlay menghancurkan value.</p>
          {picks.map((p: any, i: number) => {
            const ev = Number(p.ev) * 100
            return (
              <div key={i} className="bg-gray-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="font-bold">{p.evento}</h3>
                  <span className={`text-xs px-2 py-1 rounded ${ev >= 8 ? 'bg-emerald-900 text-emerald-300' : 'bg-yellow-900 text-yellow-300'}`}>
                    {ev >= 8 ? '🔥 Nilai Kuat' : '🟡 Nilai Positif'}
                  </span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
                  <div><span className="text-gray-400 block">🎯 Pilihan</span><b>{p.esito} @ {Number(p.quota).toFixed(2)}</b></div>
                  <div><span className="text-gray-400 block">🏦 Bandar</span>{p.bookmaker}</div>
                  <div><span className="text-gray-400 block">📈 EV</span><b className={ev >= 0 ? 'text-emerald-400' : 'text-red-400'}>+{ev.toFixed(1)}%</b></div>
                  <div><span className="text-gray-400 block">💵 Stake</span><b className="text-emerald-300">€{getStake(p).stake.toFixed(2)}</b> <span className="text-gray-500">({getStake(p).pct.toFixed(1)}%)</span></div>
                  <div><span className="text-gray-400 block">📊 Kelly{getStake(p).dynamic ? ' dinamico' : ''}</span><b>{(getStake(p).kelly * 100).toFixed(0)}%</b> <span className="text-gray-500 text-xs">{getStake(p).reason}</span></div>
                </div>
              </div>
            )
          })}
          <div className="bg-gray-900 rounded-xl p-4 text-sm flex items-center justify-between">
            <span>💵 Total investasi: <b className="text-emerald-400">€{totalStake.toFixed(2)}</b> ({((totalStake / bankroll) * 100).toFixed(1)}% bankroll)</span>
            <span className="text-gray-500 text-xs">🎯 Stake adattivo (confidence-weighted Kelly)</span>
          </div>
        </div>
      )}

      {multipla && (
        <div className="bg-gray-800 rounded-xl p-6">
          <h2 className="text-xl font-bold mb-1">🔗 Parlay Panjang</h2>
          <p className="text-amber-400 text-xs mb-4">⚠️ Hanya menang jika SEMUA pilihan masuk. Stake maks 1% bankroll.</p>
          <ol className="list-decimal list-inside mb-4 space-y-1">
            {(multipla.legs || []).map((l: any, i: number) => (
              <li key={i}>{l.esito} @ {Number(l.quota).toFixed(2)} <span className="text-gray-500 text-sm">— {l.evento}</span></li>
            ))}
          </ol>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div><span className="text-gray-400 block">💯 Total Odds</span><b>@{Number(multipla.quota).toFixed(2)}</b></div>
            <div><span className="text-gray-400 block">📈 Prob Gabungan</span>{(Number(multipla.prob) * 100).toFixed(1)}%</div>
            <div><span className="text-gray-400 block">📈 EV</span><b className={Number(multipla.ev) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
              {Number(multipla.ev) >= 0 ? '+' : ''}{(Number(multipla.ev) * 100).toFixed(1)}%
            </b></div>
            <div><span className="text-gray-400 block">💵 Stake (1/8 Kelly, cap 1%)</span>
              <b>€{(bankroll * 0.01).toFixed(2)}</b>
            </div>
          </div>
          <div className="mt-4 text-sm">
            {Number(multipla.ev) >= 0.05 ? (
              <span className="text-emerald-400">🟢 Parlay DAPAT DITERIMA (EV bagus)</span>
            ) : Number(multipla.ev) >= 0 ? (
              <span className="text-yellow-400">🟡 Parlay MARGINAL (EV ~0)</span>
            ) : (
              <span className="text-red-400">🔴 Parlay NEGATIF — tidak disarankan</span>
            )}
          </div>
        </div>
      )}
    </main>
  )
}
