'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'
import SeverityBadge, { ALERT_TYPE_META } from '../../components/SeverityBadge'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO = {
  bankroll: 500.0,
  roi_30gg: 12.4,
  segnali_oggi: 3,
  chiusi_30gg: 18,
  hit_rate: 61.1,
  ultime_value: [
    { evento: 'Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, ev: 0.155, probabilita: 0.55 },
    { evento: 'Roma vs Milan', esito: '1', quota: 2.40, ev: 0.08, probabilita: 0.45 },
  ],
  per_mercato: {},
  streaks: { current_streak: 0, current_type: 'none', max_win_streak: 0, max_loss_streak: 0 },
  clv: { n: 0, avg_raw: 0, avg_vf: 0, avg_vs_pinnacle: 0, pending: 0 },
  closing: { closed_n: 0, with_closing: 0, beat_positive: 0, avg_beat: null },
  auto_bets: { n: 0, won: 0, lost: 0, total_stake: 0, pnl: 0, roi: 0 },
  bankroll_stats: { current: 500, peak: 500, drawdown_pct: 0, risk_level: '🟢 OK' },
  market_signals: {
    summary: { total: 0, urgent: 0, by_type: { steam: 0, crash: 0, rlm: 0 } },
    signals: [],
  },
}

type Signal = { match_id: string; evento: string; esito: string; quota: number; ev?: number; alert_type: string; severity: string; total_move_pct: number; first_price?: number; last_price?: number; n_snapshots?: number; league?: string }

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/dashboard`)
        if (!res.ok) throw new Error('fallback demo')
        setData(await res.json())
      } catch {
        setData(DEMO)
        setUsingDemo(true)
      }
    })()
  }, [])

  const d = data || DEMO
  const streaks = d.streaks || {}
  const clv = d.clv || {}
  const cl = d.closing || {}
  const ab = d.auto_bets || {}
  const pm = d.per_mercato || {}
  const bs = d.bankroll_stats || {}
  const ms = d.market_signals || { summary: DEMO.market_signals.summary, signals: [] }
  const msSummary = ms.summary || {}
  const alerts: Signal[] = ms.signals || []
  const streakTxt = streaks.current_streak
    ? (streaks.current_type === 'won'
        ? `🔥 ${streaks.current_streak} vittorie di fila`
        : `📉 ${streaks.current_streak} perse di fila`)
    : '—'
  const value = d.ultime_value || []

  return (
    <main className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📊 Dashboard QuotaVerace</h1>
        <Link href="/schedina" className="text-sm bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg font-medium transition">
          📋 Schedina
        </Link>
      </div>
      {usingDemo && <p className="text-amber-400 text-sm mb-8">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}

      {/* KPI principali — saldo cassa in evidenza */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <div className="p-6 rounded-xl bg-gray-800 border-l-4 border-emerald-500 shadow-lg col-span-1 md:col-span-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-gray-400 text-sm">💰 Saldo cassa reale</span>
            <span className={`text-xs px-2 py-1 rounded ${Number(bs.drawdown_pct ?? 0) > 10 ? 'bg-red-900 text-red-300' : 'bg-emerald-900 text-emerald-300'}`}>
              {bs.risk_level || '🟢 OK'}
            </span>
          </div>
          <div className="font-bold text-3xl">€{Number(d.bankroll).toFixed(2)}</div>
          <div className="text-gray-400 text-sm mt-1">
            Peak €{Number(bs.peak ?? d.bankroll).toFixed(2)} · Drawdown{' '}
            <span className={Number(bs.drawdown_pct ?? 0) > 0 ? 'text-red-400' : 'text-emerald-400'}>
              {Number(bs.drawdown_pct ?? 0).toFixed(1)}%
            </span>
          </div>
        </div>
        <Card title="📈 ROI 30gg" value={`${Number(d.roi_30gg).toFixed(1)}%`} color="blue" />
        <Card title="🎯 Segnali Oggi" value={String(d.segnali_oggi)} color="purple" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <Card title="🔥 Streak attuale" value={streakTxt} color="orange" small />
        <Card title="✅ Hit Rate (30gg)" value={`${Number(d.hit_rate).toFixed(1)}%`} color="teal" small />
        <Card title="📈 CLV vig-free" value={clv.n ? `${Number(clv.avg_vf).toFixed(2)}%` : '—'} color="blue" small />
        <Card title="📉 Beat sul mercato (T-0)" value={cl.with_closing ? `${cl.beat_positive}/${cl.with_closing}` : '—'} color="purple" small />
      </div>

      {/* Monitor Value Bet + Movimenti di linea */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <div className="bg-gray-800 rounded-xl p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-bold">🎯 Value Bet attive</h2>
            <Link href="/value" className="text-sm text-blue-400 hover:underline">Tutte →</Link>
          </div>
          {value.length === 0 ? (
            <p className="text-gray-500 text-sm">Nessuna value bet in corso.</p>
          ) : (
            <div className="space-y-3">
              {value.slice(0, 5).map((v: any, i: number) => {
                const ev = Number(v.ev) * 100
                const strong = ev >= 8
                return (
                  <div key={i} className={`bg-gray-900 rounded-lg p-4 border-l-4 ${strong ? 'border-orange-500' : 'border-emerald-500'}`}>
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="font-medium truncate">{v.evento}</span>
                      <SeverityBadge type={strong ? 'strong' : 'value'} />
                    </div>
                    <div className="text-sm text-gray-400">
                      {v.esito} @ <b className="text-gray-200">{Number(v.quota).toFixed(2)}</b>
                      <span className="mx-2">·</span>
                      EV <b className={ev >= 0 ? 'text-emerald-400' : 'text-red-400'}>+{ev.toFixed(1)}%</b>
                      {v.probabilita != null && (
                        <>
                          <span className="mx-2">·</span>
                          Prob {(Number(v.probabilita) * 100).toFixed(0)}%
                        </>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <div className="bg-gray-800 rounded-xl p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-bold">📉 Movimenti di linea</h2>
            <Link href="/movimenti" className="text-sm text-blue-400 hover:underline">Dettaglio →</Link>
          </div>
          <div className="grid grid-cols-3 gap-3 mb-4">
            <div className="p-3 rounded-lg bg-gray-900 border-l-4 border-orange-500 text-center">
              <div className="text-gray-400 text-sm">🔥 Steam</div>
              <div className="text-xl font-bold">{msSummary.by_type?.steam ?? 0}</div>
            </div>
            <div className="p-3 rounded-lg bg-gray-900 border-l-4 border-red-500 text-center">
              <div className="text-gray-400 text-sm">🚨 Crollo</div>
              <div className="text-xl font-bold">{msSummary.by_type?.crash ?? 0}</div>
            </div>
            <div className="p-3 rounded-lg bg-gray-900 border-l-4 border-yellow-500 text-center">
              <div className="text-gray-400 text-sm">⚠️ RLM</div>
              <div className="text-xl font-bold">{msSummary.by_type?.rlm ?? 0}</div>
            </div>
          </div>
          {alerts.length === 0 ? (
            <p className="text-gray-500 text-sm">Nessun movimento rilevante sui segnali attivi.<br />
              <span className="text-xs">Job ogni 5&apos; dalle 14:00 — movimenti &gt; 3% compaiono qui.</span>
            </p>
          ) : (
            <div className="space-y-3 max-h-72 overflow-y-auto pr-1">
              {alerts.slice(0, 5).map((a, i) => {
                const meta = ALERT_TYPE_META[a.alert_type] || ALERT_TYPE_META.rlm
                const move = Number(a.total_move_pct)
                return (
                  <div key={i} className={`bg-gray-900 rounded-lg p-4 border-l-4 ${meta.border}`}>
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="font-medium text-sm truncate">{a.evento}</span>
                      <span className="flex items-center gap-1.5 shrink-0">
                        <span className={`text-xs font-bold px-2 py-0.5 rounded bg-gray-700 text-gray-200`}>
                          {meta.emoji} {meta.label}
                        </span>
                        <SeverityBadge type={a.severity === 'urgent' ? 'urgent' : 'warning'} />
                      </span>
                    </div>
                    <div className="text-sm text-gray-400">
                      {a.esito} @ <b className="text-gray-200">{Number(a.quota).toFixed(2)}</b>
                      <span className="mx-2">·</span>
                      <b className={move < 0 ? 'text-red-400' : 'text-emerald-400'}>
                        {move > 0 ? '↗' : '↙'} {move > 0 ? '+' : ''}{move.toFixed(1)}%
                      </b>
                      <span className="mx-2">·</span>
                      {Number(a.first_price).toFixed(2)} → {Number(a.last_price).toFixed(2)}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        {/* Calibrazione per mercato */}
        <div className="bg-gray-800 rounded-xl p-6">
          <h2 className="text-xl font-bold mb-4">📚 Calibrazione per mercato</h2>
          {Object.keys(pm).length === 0 && <p className="text-gray-500 text-sm">Nessuna previsione chiusa ancora.</p>}
          <table className="w-full text-left">
            <thead><tr className="text-gray-400 border-b border-gray-600"><th className="pb-2">Mercato</th><th className="pb-2 text-right">N</th><th className="pb-2 text-right">Hit</th><th className="pb-2 text-right">ROI</th><th className="pb-2 text-right">EV att.</th></tr></thead>
            <tbody>
              {Object.entries(pm).map(([mkt, b]: [string, any]) => (
                <tr key={mkt} className="border-b border-gray-700">
                  <td className="py-2 font-medium">{mkt}</td>
                  <td className="py-2 text-right">{b.n}</td>
                  <td className="py-2 text-right">{Number(b.hit_rate).toFixed(0)}%</td>
                  <td className={`py-2 text-right font-bold ${Number(b.roi) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{Number(b.roi).toFixed(1)}%</td>
                  <td className="py-2 text-right text-gray-400">{Number(b.avg_ev).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Puntate automatiche + CLV */}
        <div className="bg-gray-800 rounded-xl p-6">
          <h2 className="text-xl font-bold mb-4">🤖 Puntate automatiche (30gg)</h2>
          {ab.n === 0 ? <p className="text-gray-500 text-sm">Nessuna puntata chiusa nel periodo.</p> : (
            <>
              <div className="grid grid-cols-3 gap-3 mb-4">
                <MiniStat label="Chiuse" value={String(ab.n)} />
                <MiniStat label="Vinte" value={String(ab.won ?? 0)} />
                <MiniStat label="P/L" value={`€${Number(ab.pnl ?? 0).toFixed(2)}`} color={Number(ab.pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'} />
              </div>
              <p className="text-gray-400 text-sm">
                Stake totale €{Number(ab.total_stake ?? 0).toFixed(2)} · ROI <b className={Number(ab.roi ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(ab.roi ?? 0).toFixed(1)}%</b>
              </p>
            </>
          )}
          <h3 className="text-lg font-bold mt-6 mb-2">🎯 CLV (30gg)</h3>
          <div className="text-sm text-gray-400 space-y-1">
            <p>Raw: <b className={Number(clv.avg_raw ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_raw ?? 0).toFixed(2)}%</b> (n {clv.n ?? 0})</p>
            <p>Vig-free: <b className={Number(clv.avg_vf ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_vf ?? 0).toFixed(2)}%</b></p>
            <p>vs Pinnacle: <b className={Number(clv.avg_vs_pinnacle ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_vs_pinnacle ?? 0).toFixed(2)}%</b></p>
            {Number(clv.pending ?? 0) > 0 && <p className="text-amber-400">⏳ {clv.pending} segnali in attesa di chiusura</p>}
          </div>
          {/* BEAT SUL MERCATO (direttiva 04/10): la closing line Pinnacle
              catturata a T-0 e' la prova che il prezzo battuto era quello
              giusto, non solo un CLV stimato. */}
          <h3 className="text-lg font-bold mt-6 mb-2">📉 Closing line Pinnacle (T-0)</h3>
          {!(cl.with_closing ?? 0) ? (
            <p className="text-gray-500 text-sm">Nessun campione di closing line catturato finora.
              <br /><span className="text-xs">La routine gira ogni 5&apos; e cattura lo sharp nei minuti del fischio (T-10..T+5).</span>
            </p>
          ) : (
            <div className="text-sm text-gray-400 space-y-1">
              <p>Campioni con closing: <b className="text-gray-200">{cl.with_closing}</b> su {cl.closed_n ?? 0} righe CLV</p>
              <p>Closing battuta (beat positivo): <b className={Number(cl.beat_positive ?? 0) > 0 ? 'text-emerald-400' : 'text-gray-200'}>
                {cl.beat_positive ?? 0}/{cl.with_closing}</b>{' '}
                <span className="text-gray-500">({((Number(cl.beat_positive ?? 0) / Number(cl.with_closing || 1)) * 100).toFixed(0)}%)</span>
              </p>
              {cl.avg_beat != null && (
                <p>Beat medio: <b className={Number(cl.avg_beat) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                  {(Number(cl.avg_beat) * 100).toFixed(2)}%</b></p>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  )
}

function Card({ title, value, color, small }: { title: string; value: string; color: string; small?: boolean }) {
  const bc = color === 'emerald' ? 'border-emerald-500' : color === 'blue' ? 'border-blue-500' : color === 'teal' ? 'border-teal-500' : color === 'orange' ? 'border-orange-500' : color === 'red' ? 'border-red-500' : 'border-purple-500'
  return <div className={`p-6 rounded-xl bg-gray-800 border-l-4 ${bc} shadow-lg`}><div className="text-gray-400 text-sm mb-1">{title}</div><div className={`font-bold ${small ? 'text-xl' : 'text-3xl'}`}>{value}</div></div>
}

function MiniStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return <div className="bg-gray-900 p-3 rounded-lg text-center"><div className="text-gray-400 text-sm">{label}</div><div className={`text-xl font-bold ${color || ''}`}>{value}</div></div>
}