'use client'
import { Fragment, useCallback, useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''
const REFRESH_MS = 15000

type Metric = { key: string; label: string; value: any; unit?: string; tone?: string }
type AgentCard = {
  id: string
  name: string
  role: string
  consumes: string
  produces: string
  isolation: string
  status: string
  detail?: string
  last_seen?: string | null
  metrics: Metric[]
  window?: { cycles: number; with_output: number; last_seen?: string | null }
}
type Check = { id: string; label: string; status: string; active: boolean; detail: string }
type Board = {
  generated_at?: string
  window_days?: number
  last_cycle?: { ts?: string; ok?: boolean; blocked_reason?: string; bankroll?: number; mode?: string }
  cycles?: { window: number; lines_read: number; log: string; error?: string }
  agents: AgentCard[]
  safety?: { checks: Check[]; blocked_by: string[]; error?: string }
  capital?: any
  flow?: any
  composer?: { name: string; rule: string; mode: string; shadow: boolean }
  cached?: boolean
  error?: string
}

const DEMO: Board = {
  generated_at: '2026-10-19T00:00:00Z',
  window_days: 7,
  last_cycle: { ts: '—', ok: false, blocked_reason: 'backend non raggiungibile', mode: 'live' },
  agents: [
    { id: 'data', name: 'Dati & Mercato', role: 'Feed sharp + segnali', consumes: 'ledger', produces: 'MarketData', isolation: 'sola lettura', status: 'unknown', detail: 'dati dimostrativi', metrics: [], window: { cycles: 0, with_output: 0 } },
  ],
  safety: { checks: [], blocked_by: [] },
  capital: { available: false },
  flow: {},
}

const STATUS: Record<string, { label: string; cls: string }> = {
  ok: { label: 'OK', cls: 'bg-emerald-900/60 text-emerald-300 border-emerald-700' },
  skipped: { label: 'SALTATO', cls: 'bg-amber-900/50 text-amber-300 border-amber-700' },
  idle: { label: 'IN ATTESA', cls: 'bg-gray-700/60 text-gray-300 border-gray-600' },
  error: { label: 'ERRORE', cls: 'bg-red-900/60 text-red-300 border-red-700' },
  unknown: { label: 'SCONOSCIUTO', cls: 'bg-gray-700/60 text-gray-400 border-gray-600' },
}

function toneCls(tone?: string) {
  if (tone === 'good') return 'text-emerald-300'
  if (tone === 'warn') return 'text-amber-300'
  if (tone === 'bad') return 'text-red-300'
  return 'text-gray-200'
}

function showValue(m: Metric) {
  const v = m.value
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'number') {
    const txt = Number.isInteger(v) ? String(v) : v.toFixed(2)
    return m.unit ? `${txt} ${m.unit}` : txt
  }
  if (typeof v === 'object') {
    const entries = Object.entries(v)
    if (!entries.length) return '—'
    return entries.map(([k, n]) => `${k}: ${n}`).join(', ')
  }
  return `${v}${m.unit ? ' ' + m.unit : ''}`
}

export default function Agenti() {
  const [data, setData] = useState<Board | null>(null)
  const [usingDemo, setUsingDemo] = useState(false)
  const [updated, setUpdated] = useState<string>('')
  const [open, setOpen] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/agents`, { cache: 'no-store' })
      if (!res.ok) throw new Error('fallback')
      const json = await res.json()
      if (json?.error && !(json.agents || []).length) throw new Error('errore')
      setData(json)
      setUsingDemo(false)
    } catch {
      setData(DEMO)
      setUsingDemo(true)
    } finally {
      setUpdated(new Date().toLocaleTimeString('it-IT'))
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  const d = data || DEMO
  const last = d.last_cycle || {}
  const safety = d.safety || { checks: [], blocked_by: [] }
  const blocked = safety.blocked_by || []
  const cap = d.capital || {}
  const agents = d.agents || []
  const cycleError = d.cycles?.error
  const decisions = d.flow?.decisions || {}

  return (
    <main className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-3">
        <h1 className="text-3xl font-bold">🏛️ Tabella di comando</h1>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500">aggiornata {updated || '—'}</span>
          <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
        </div>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        Ogni agente lavora per conto suo: passa tutto dal Capo. Qui si vede chi ha
        prodotto cosa nell'ultimo ciclo e cosa lo ha fermato.
      </p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}
      {cycleError && <p className="text-amber-400 text-sm mb-6">⚠️ registro cicli: {cycleError}</p>}

      {/* Stato del ciclo + sicurezza */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className={`p-5 rounded-xl bg-gray-800 border-l-4 ${last.ok ? 'border-emerald-500' : 'border-red-500'}`}>
          <div className="text-gray-400 text-sm mb-1">Ultimo ciclo del Capo</div>
          <div className="text-lg font-semibold">{last.ok ? '✅ completato' : '🛑 fermato'}</div>
          <div className="text-xs text-gray-400 mt-1">{last.ts || '—'}</div>
          {!last.ok && last.blocked_reason && (
            <div className="text-xs text-red-300 mt-2 break-words">{last.blocked_reason}</div>
          )}
          <div className="text-xs text-gray-500 mt-2">
            modo {last.mode || '—'} · bankroll {typeof last.bankroll === 'number' ? last.bankroll.toFixed(2) + ' USDC' : '—'}
            {' · '}{d.cycles?.window ?? 0} cicli in {d.window_days ?? 7}gg
          </div>
        </div>

        <div className="p-5 rounded-xl bg-gray-800">
          <div className="text-gray-400 text-sm mb-2">🛡 Sicurezza</div>
          {blocked.length === 0
            ? <div className="text-emerald-300 text-sm">Nessun blocco attivo</div>
            : <div className="text-red-300 text-sm mb-2">Blocchi: {blocked.join(', ')}</div>}
          <ul className="text-xs text-gray-300 space-y-1 mt-2">
            {(safety.checks || []).map(c => (
              <li key={c.id} className="flex items-start gap-2">
                <span>{c.active ? '🛑' : '🟢'}</span>
                <span className="flex-1">
                  <span className="text-gray-200">{c.label}</span>
                  {c.detail && <span className="text-gray-500"> — {c.detail}</span>}
                </span>
              </li>
            ))}
            {!(safety.checks || []).length && <li className="text-gray-500">stato non disponibile</li>}
          </ul>
        </div>

        <div className="p-5 rounded-xl bg-gray-800">
          <div className="text-gray-400 text-sm mb-2">💼 Capitale</div>
          {cap.available ? (
            <>
              <div className="text-2xl font-bold text-emerald-300">{Number(cap.equity || 0).toFixed(2)} <span className="text-sm text-gray-400">USDC</span></div>
              <div className="text-xs text-gray-400 mt-2">
                impegnato {(Number(cap.open_stake || 0)).toFixed(2)} / {(Number(cap.cap || 0)).toFixed(2)}
                {' '}({Math.round(100 * Number(cap.used_pct || 0))}%) · {cap.open_orders ?? 0} ordini
              </div>
              <div className="text-xs text-gray-400 mt-1">
                tetto per ordine <span className="text-gray-200">{Number(cap.order_ceiling || 0).toFixed(2)}</span>
                {cap.hard_cap_per_order ? <span className="text-gray-500"> (assoluto {Number(cap.hard_cap_per_order).toFixed(2)})</span> : null}
              </div>
            </>
          ) : (
            <div className="text-sm text-gray-500">{cap.error || 'equity non leggibile'}</div>
          )}
        </div>
      </div>

      {/* La tabella */}
      <div className="rounded-xl bg-gray-800 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-900/60 text-gray-400 text-xs uppercase">
            <tr>
              <th className="text-left p-3 w-10">#</th>
              <th className="text-left p-3 min-w-[180px]">Agente</th>
              <th className="text-left p-3 w-28">Stato</th>
              <th className="text-left p-3">Indicatori</th>
              <th className="text-left p-3 w-24">Finestra</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a, i) => {
              const st = STATUS[a.status] || STATUS.unknown
              const expanded = open === a.id
              return (
                <Fragment key={a.id}>
                  <tr className="border-t border-gray-700 align-top cursor-pointer hover:bg-gray-750/40"
                      onClick={() => setOpen(expanded ? null : a.id)}>
                    <td className="p-3 text-gray-500">{i + 1}</td>
                    <td className="p-3">
                      <div className="font-medium text-gray-100">{a.name}</div>
                      <div className="text-xs text-gray-500">{a.role}</div>
                      {a.detail && <div className="text-xs text-amber-300 mt-1">{a.detail}</div>}
                    </td>
                    <td className="p-3">
                      <span className={`px-2 py-1 rounded border text-xs ${st.cls}`}>{st.label}</span>
                    </td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-x-4 gap-y-1">
                        {(a.metrics || []).map(m => (
                          <span key={m.key} className="text-xs">
                            <span className="text-gray-500">{m.label}: </span>
                            <span className={toneCls(m.tone)}>{showValue(m)}</span>
                          </span>
                        ))}
                        {!(a.metrics || []).length && <span className="text-xs text-gray-500">nessun indicatore nell'ultimo ciclo</span>}
                      </div>
                    </td>
                    <td className="p-3 text-xs text-gray-400">
                      {a.window ? `${a.window.with_output}/${a.window.cycles}` : '—'}
                      <div className="text-gray-600">{expanded ? 'chiudi' : 'dettagli'}</div>
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="bg-gray-900/40">
                      <td />
                      <td colSpan={4} className="p-4 text-xs text-gray-300 space-y-1">
                        <div><span className="text-gray-500">Legge:</span> {a.consumes}</div>
                        <div><span className="text-gray-500">Produce:</span> {a.produces}</div>
                        <div><span className="text-gray-500">Isolamento:</span> {a.isolation}</div>
                        <div><span className="text-gray-500">Ultimo output:</span> {a.window?.last_seen || a.last_seen || '—'}</div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {!agents.length && (
              <tr><td colSpan={5} className="p-6 text-center text-gray-500">
                Nessun ciclo registrato: il Capo gira dentro il giro dei 60s
                (<code>CHIEF_SHADOW_ENABLED</code>). Se resta vuoto, il registro
                non e' scrivibile.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Flusso */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-6 text-sm">
        <div className="p-5 rounded-xl bg-gray-800">
          <div className="text-gray-400 mb-2">🧭 Decisioni della catena</div>
          {decisions.error
            ? <div className="text-amber-300 text-xs">{decisions.error}</div>
            : (
              <div className="text-xs text-gray-300 space-y-1">
                <div>totale <span className="text-gray-100">{decisions.n ?? 0}</span> · aperte {decisions.open ?? 0} · con ordine {decisions.with_order ?? 0}</div>
                <div className="text-gray-500">verdetti: {JSON.stringify(decisions.by_verdict || {})}</div>
                <div className="text-gray-500">stati: {JSON.stringify(decisions.by_status || {})}</div>
              </div>
            )}
        </div>
        <div className="p-5 rounded-xl bg-gray-800">
          <div className="text-gray-400 mb-2">🧾 Refertazione</div>
          <div className="text-xs text-gray-300 space-y-1">
            <div>puntate aperte <span className="text-gray-100">{d.flow?.settlement?.bets_open ?? '—'}</span> · previsioni aperte {d.flow?.settlement?.predictions_open ?? '—'}</div>
            <div>insaldabili oltre soglia <span className={(d.flow?.settlement?.overdue_orphans || 0) > 0 ? 'text-red-300' : 'text-emerald-300'}>{d.flow?.settlement?.overdue_orphans ?? '—'}</span></div>
            <div className="text-gray-500">crediti attesi: {d.flow?.settlement?.estimated_credits ?? '—'}</div>
          </div>
        </div>
      </div>

      <p className="text-xs text-gray-500 mt-6">
        {d.composer?.rule || 'i dati passano solo dal Capo'} · sorgente: <code>{d.cycles?.log || '—'}</code>
        {d.cached ? ' · istantanea in cache (15s)' : ''}
      </p>
    </main>
  )
}
