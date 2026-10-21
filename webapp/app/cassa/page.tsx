'use client'
import { useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { expectedGoals, prob1x2, probOverUnder, probBtts, computeEv, TEAM_RATINGS } from '../../lib/poisson'
import { CASSA_ENDPOINT, deleteCassa, loadState, postCassa } from '../../lib/cassaClient'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''
const STORAGE_KEY = 'qv_cassa_v1'

type CassaRow = {
  id: string
  data: string
  partita: string
  esito: string
  quota: number
  importo: number
  ev: number
  prob: number
  esito_finale?: string | null
  profit?: number | null
  settled_at?: string | null
}

type Team = { nome: string; campionato: string }

const ESITI = [
  { key: '1', label: '1 — Casa' },
  { key: 'X', label: 'X — Pareggio' },
  { key: '2', label: '2 — Trasferta' },
  { key: 'Over 2.5', label: 'Over 2.5' },
  { key: 'Under 2.5', label: 'Under 2.5' },
  { key: 'BTTS', label: 'Gol Gol (BTTS)' },
]

export default function Cassa() {
  const [teams, setTeams] = useState<Team[]>([])
  const [home, setHome] = useState('')
  const [away, setAway] = useState('')
  const [homeSuggest, setHomeSuggest] = useState<Team[]>([])
  const [awaySuggest, setAwaySuggest] = useState<Team[]>([])
  const [activeField, setActiveField] = useState<'home' | 'away' | null>(null)
  const [signal, setSignal] = useState<any>(null)
  const [esito, setEsito] = useState('1')
  const [quota, setQuota] = useState('2.00')
  const [importo, setImporto] = useState('10')
  const [cassa, setCassa] = useState<CassaRow[]>([])
  const [syncState, setSyncState] = useState<'ok' | 'locale' | 'offline' | 'errore' | 'loading'>('loading')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const boxRef = useRef<HTMLDivElement>(null)

  // Carica campionati (con fallback locale dal motore Poisson)
  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/campionati`)
        if (res.ok) {
          const j = await res.json()
          const flat: Team[] = []
          for (const c of j.campionati || []) {
            for (const s of c.squadre || []) flat.push({ nome: s, campionato: c.nome })
          }
          if (flat.length) { setTeams(flat); return }
        }
      } catch { /* fallback sotto */ }
      // Fallback: squadre note dal motore client-side (funziona offline)
      const fallback: Team[] = Object.keys(TEAM_RATINGS || {}).map(k => ({
        nome: k.split(' ').map((w: string) => w[0]?.toUpperCase() + w.slice(1)).join(' '),
        campionato: 'Top leghe',
      }))
      setTeams(fallback)
    })()
    loadCassa()
  }, [])

  // Carica cassa: mostra subito la copia locale, ma lo stato `ok` arriva SOLO
  // dal server. Senza conferma remota lo stato e' `locale` (dati non allineati)
  // oppure `errore`/`offline`: mai "sincronizzato" per un dato solo locale.
  async function loadCassa() {
    let localRows: CassaRow[] = []
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) {
        localRows = JSON.parse(raw)
        if (localRows.length) setCassa(localRows)
      }
    } catch { /* copia locale illeggibile: si resta sul server */ }
    try {
      const res = await fetch(CASSA_ENDPOINT, { cache: 'no-store' })
      if (!res.ok) {
        setSyncState(loadState({
          serverOk: false, serverRows: 0, localRows: localRows.length,
        }))
        return
      }
      const j: any = await res.json().catch(() => null)
      if (!j || typeof j !== 'object') {
        setSyncState(loadState({
          serverOk: false, serverRows: 0, localRows: localRows.length,
        }))
        return
      }
      const rows = (j.scommesse || []).map((s: any) => ({
        id: String(s.id), data: s.data, partita: s.partita, esito: s.esito,
        quota: Number(s.quota), importo: Number(s.importo), ev: Number(s.ev || 0), prob: 0,
        esito_finale: s.esito_finale || null,
        profit: s.profit != null ? Number(s.profit) : null,
        settled_at: s.settled_at || null,
      }))
      if (rows.length) { setCassa(rows); persistLocal(rows) }
      setSyncState(loadState({
        serverOk: true, serverRows: rows.length, localRows: localRows.length,
      }))
    } catch {
      setSyncState(loadState({
        serverOk: false, serverRows: 0, localRows: localRows.length,
        networkError: true,
      }))
    }
  }

  function persistLocal(rows: CassaRow[]) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(rows)) } catch { /* ignore */ }
  }

  // Autocomplete
  const filterTeams = (query: string) => {
    const q = query.toLowerCase()
    return teams.filter(t => t.nome.toLowerCase().includes(q)).slice(0, 8)
  }
  useEffect(() => {
    if (activeField === 'home') setHomeSuggest(home ? filterTeams(home) : [])
    else if (activeField === 'away') setAwaySuggest(away ? filterTeams(away) : [])
  }, [home, away, teams, activeField])

  // Click fuori chiude i suggerimenti
  useEffect(() => {
    const h = (e: MouseEvent) => { if (boxRef.current && !boxRef.current.contains(e.target as Node)) setActiveField(null) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  // Calcola il segnale quando entrambe le squadre sono selezionate
  function calcSignal(h: string, a: string) {
    const [lamH, lamA] = expectedGoals(h, a)
    if (lamH === null || lamA === null) {
      setSignal({ error: `Squadra non riconosciuta. Controlla il nome (es. "Inter", "Manchester City").` })
      return
    }
    const [p1, px, p2] = prob1x2(lamH, lamA)
    const [pOver, pUnder] = probOverUnder(lamH, lamA)
    const btts = probBtts(lamH, lamA)
    const cands: { key: string; prob: number; quota: number; ev: number }[] = [
      { key: '1', prob: p1, quota: 2.0, ev: 0 }, { key: 'X', prob: px, quota: 3.2, ev: 0 },
      { key: '2', prob: p2, quota: 2.0, ev: 0 }, { key: 'Over 2.5', prob: pOver, quota: 2.1, ev: 0 },
      { key: 'Under 2.5', prob: pUnder, quota: 1.85, ev: 0 }, { key: 'BTTS', prob: btts, quota: 1.9, ev: 0 },
    ]
    cands.forEach(c => { c.ev = computeEv(c.prob, c.quota) })
    const best = [...cands].sort((x, y) => y.ev - x.ev)[0]
    setSignal({ lamH, lamA, p1, px, p2, pOver, pUnder, btts, cands, best })
    setEsito(best.key)
    setQuota(best.quota.toFixed(2))
  }

  function selectTeam(field: 'home' | 'away', name: string) {
    if (field === 'home') { setHome(name); setHomeSuggest([]); setActiveField('away') }
    else { setAway(name); setAwaySuggest([]); setActiveField(null) }
    if (field === 'home' && away) { setHome(name); calcSignal(name, away) }
    if (field === 'away' && home) { setAway(name); calcSignal(home, name) }
  }

  const cand = signal?.cands?.find((c: any) => c.key === esito)
  const quotaNum = parseFloat(quota) || 0
  const importoNum = parseFloat(importo) || 0
  const vincita = quotaNum > 0 ? importoNum * quotaNum : 0
  const profit = vincita - importoNum

  async function addToCassa() {
    if (busy) return
    if (!home || !away) { setNotice('⚠️ Inserisci prima la partita (cerca casa e trasferta).'); return }
    if (quotaNum <= 1) { setNotice('⚠️ Quota non valida (deve essere > 1).'); return }
    if (importoNum <= 0) { setNotice('⚠️ Inserisci l\'importo da scommettere (€).'); return }
    const row: CassaRow = {
      id: `${Date.now()}`,
      data: new Date().toISOString().slice(0, 10),
      partita: `${home} vs ${away}`,
      esito, quota: quotaNum, importo: importoNum,
      ev: cand?.ev || 0, prob: cand?.prob || 0,
      esito_finale: null, profit: null, settled_at: null,
    }
    setBusy(true)
    const res = await postCassa({
      partita: row.partita, esito: row.esito, quota: row.quota,
      importo: row.importo, ev: row.ev, data: row.data,
    })
    setBusy(false)
    if (!res.ok) {
      // P2: il server NON ha salvato. La cassa locale non viene toccata e il
      // messaggio di successo NON compare (prima il fallimento era silenzioso).
      setSyncState(res.status === 0 ? 'offline' : 'errore')
      setNotice(`❌ NON salvata sul server (${res.status ? `HTTP ${res.status}` : 'rete'}): ${res.message}. La cassa locale non è stata modificata.`)
      return
    }
    const rows = [row, ...cassa]
    setCassa(rows); persistLocal(rows); setSyncState('ok')
    setNotice(`✅ ${home} vs ${away} — ${esito} @ ${quotaNum.toFixed(2)} con €${importoNum.toFixed(2)} aggiunta alla cassa (salvata sul server).`)
    // reset per la prossima
    setHome(''); setAway(''); setSignal(null); setImporto('10')
  }

  function removeRow(id: string) {
    // Il backend espone solo lo svuotamento totale: non esiste una DELETE per
    // singola riga. Rimuovere in locale e riscrivere tutto il server (DELETE +
    // POST) poteva lasciare la cassa server vuota o parziale se un POST
    // falliva: qui si tocca SOLO il dispositivo e lo si dichiara.
    const rows = cassa.filter(r => r.id !== id)
    setCassa(rows); persistLocal(rows)
    setNotice('ℹ️ Riga rimossa solo da questo dispositivo: il backend non supporta la cancellazione di una singola riga. Per allineare il server usa 🗑 Svuota (cancella tutto, con conferma) e reinserisci le righe.')
  }

  async function clearAll() {
    if (busy) return
    // Conferma ESPLICITA dell'utente: la frase richiesta dal backend va scritta.
    // Non viene mai inviata da sola e non esiste un percorso che la aggiri.
    const typed = window.prompt(
      'Questa operazione SVUOTA LA CASSA SUL SERVER (il backend crea un backup prima di cancellare).\n' +
      'Scrivi CANCELLA_CASSA per confermare:'
    )
    if (typed === null) return
    if (typed.trim() !== 'CANCELLA_CASSA') {
      setNotice('❌ Operazione annullata: testo di conferma non corretto. Nulla è stato cancellato.')
      return
    }
    setBusy(true)
    const res = await deleteCassa('CANCELLA_CASSA')
    setBusy(false)
    if (!res.ok) {
      setSyncState(res.status === 0 ? 'offline' : 'errore')
      setNotice(`❌ Cassa NON svuotata sul server (${res.status ? `HTTP ${res.status}` : 'rete'}): ${res.message}. I dati locali sono invariati.`)
      return
    }
    setCassa([]); persistLocal([]); setSyncState('ok')
    setNotice('🗑 Cassa svuotata sul server (backup creato dal backend).')
  }

  const totali = useMemo(() => {
    const speso = cassa.reduce((s, r) => s + r.importo, 0)
    const chiusi = cassa.filter(r => r.esito_finale)
    const inGioco = cassa.filter(r => !r.esito_finale)
    const vincPot = inGioco.reduce((s, r) => s + r.importo * r.quota, 0)
    const profitReale = chiusi.reduce((s, r) => s + (r.profit ?? 0), 0)
    const spesoChiuso = chiusi.reduce((s, r) => s + r.importo, 0)
    const roi = spesoChiuso > 0 ? (profitReale / spesoChiuso) * 100 : 0
    return { n: cassa.length, speso, vincita: vincPot, profit: profitReale, roi, chiusi: chiusi.length, inGioco: inGioco.length }
  }, [cassa])

  const esitoLabel = (key: string) => ESITI.find(e => e.key === key)?.label || key

  return (
    <main className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">💰 Cassa Scommesse</h1>
        <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        Inserisci la partita, scegli l'esito, indica l'importo: il calcolatore ti mostra la <b className="text-emerald-400">vincita probabile in euro</b>.
        {syncState === 'offline' && <span className="text-amber-400"> · server non raggiungibile (dati solo su questo dispositivo)</span>}
        {syncState === 'locale' && (
          <span className="text-amber-400"> · dati solo su questo dispositivo, non confermati dal server</span>
        )}
        {syncState === 'errore' && (
          <span className="text-red-400"> · sincronizzazione non riuscita
            <button onClick={() => loadCassa()} className="ml-2 underline hover:text-red-300">riprova</button>
          </span>
        )}
      </p>

      {/* ── Ricerca partita ── */}
      <div ref={boxRef} className="bg-gray-800 rounded-xl p-6 mb-6">
        <h2 className="text-xl font-bold mb-4">🔍 1 · Cerca la partita</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {(['home', 'away'] as const).map(field => (
            <div key={field} className="relative">
              <label className="block text-gray-400 text-sm mb-1">{field === 'home' ? '🏠 Casa' : '✈️ Trasferta'}</label>
              <input
                value={field === 'home' ? home : away}
                onChange={e => field === 'home' ? setHome(e.target.value) : setAway(e.target.value)}
                onFocus={() => setActiveField(field)}
                placeholder={field === 'home' ? 'Es. Inter' : 'Es. Napoli'}
                className="w-full p-3 rounded-lg bg-gray-900 border border-gray-600 focus:border-emerald-500 outline-none"
              />
              {(field === 'home' ? homeSuggest : awaySuggest).length > 0 && (
                <div className="absolute z-20 w-full mt-1 bg-gray-900 border border-gray-600 rounded-lg shadow-xl max-h-56 overflow-auto">
                  {(field === 'home' ? homeSuggest : awaySuggest).map(t => (
                    <button key={`${t.nome}-${t.campionato}`} onClick={() => selectTeam(field, t.nome)}
                      className="block w-full text-left px-4 py-2 hover:bg-gray-700 transition">
                      <span className="font-medium">{t.nome}</span>
                      <span className="text-gray-500 text-xs ml-2">{t.campionato}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* ── Segnale ── */}
      <div className="bg-gray-800 rounded-xl p-6 mb-6">
        <h2 className="text-xl font-bold mb-4">🎯 2 · Segnale trovato</h2>
        {!signal && <p className="text-gray-500">Cerca le due squadre sopra: qui compaiono probabilità, esito consigliato e quota.</p>}
        {signal?.error && <p className="text-red-400">{signal.error}</p>}
        {signal && !signal.error && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <ProbBox label="1" value={signal.p1} />
              <ProbBox label="X" value={signal.pX} />
              <ProbBox label="2" value={signal.p2} />
              <ProbBox label="Over 2.5" value={signal.pOver} />
              <ProbBox label="Under 2.5" value={signal.pUnder} />
              <ProbBox label="BTTS" value={signal.btts} />
              <ProbBox label="λ Casa" value={signal.lamH} raw />
              <ProbBox label="λ Trasferta" value={signal.lamA} raw />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
              <div>
                <label className="block text-gray-400 text-sm mb-1">Esito</label>
                <select value={esito} onChange={e => setEsito(e.target.value)}
                  className="w-full p-3 rounded-lg bg-gray-900 border border-gray-600 focus:border-emerald-500 outline-none">
                  {signal.cands.map((c: any) => (
                    <option key={c.key} value={c.key}>{esitoLabel(c.key)} — prob {(c.prob * 100).toFixed(1)}% — EV {(c.ev * 100).toFixed(1)}%</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-gray-400 text-sm mb-1">Quota (modificabile)</label>
                <input value={quota} onChange={e => setQuota(e.target.value)} inputMode="decimal"
                  className="w-full p-3 rounded-lg bg-gray-900 border border-gray-600 focus:border-emerald-500 outline-none" />
              </div>
              <div>
                <label className="block text-gray-400 text-sm mb-1">💶 Importo scommesso (€)</label>
                <input value={importo} onChange={e => setImporto(e.target.value)} inputMode="decimal"
                  className="w-full p-3 rounded-lg bg-gray-900 border border-emerald-500 outline-none" />
              </div>
            </div>

            <div className="mt-4 bg-emerald-900/30 border border-emerald-500/30 p-4 rounded-lg flex flex-wrap items-center justify-between gap-4">
              <div>
                <div className="text-gray-400 text-sm">Vincita probabile</div>
                <div className="text-3xl font-bold text-emerald-400">€{vincita.toFixed(2)}</div>
                <div className="text-sm text-gray-400">€{importoNum.toFixed(2)} × {quotaNum.toFixed(2)} · profitto <b className={profit >= 0 ? 'text-emerald-400' : 'text-red-400'}>€{profit.toFixed(2)}</b></div>
              </div>
              <button onClick={addToCassa} disabled={busy} className="px-6 py-3 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg font-bold transition">
                {busy ? '⏳ Salvataggio…' : '➕ Aggiungi alla cassa'}
              </button>
            </div>
          </>
        )}
        {notice && <p className="mt-4 text-sm text-amber-300">{notice}</p>}
      </div>

      {/* ── Tabella cassa ── */}
      <div className="bg-gray-800 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between p-4 border-b border-gray-700">
          <h2 className="text-xl font-bold">📒 3 · Tabella cassa ({totali.n})</h2>
          {totali.n > 0 && <button onClick={clearAll} disabled={busy} className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50">🗑 Svuota</button>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead className="bg-gray-700">
              <tr>
                <th className="p-3">Data</th><th className="p-3">Partita</th><th className="p-3">Esito</th>
                <th className="p-3">Quota</th><th className="p-3">Importo</th><th className="p-3">Vinto / pot.</th>
                <th className="p-3">Outcome</th><th className="p-3">EV</th><th className="p-3"></th>
              </tr>
            </thead>
            <tbody>
              {cassa.map(r => (
                <tr key={r.id} className="border-b border-gray-700 hover:bg-gray-700/50 transition">
                  <td className="p-3 text-gray-400 text-sm">{r.data}</td>
                  <td className="p-3 font-medium">{r.partita}</td>
                  <td className="p-3">{esitoLabel(r.esito)}</td>
                  <td className="p-3">{r.quota.toFixed(2)}</td>
                  <td className="p-3">€{r.importo.toFixed(2)}</td>
                  {r.esito_finale ? (
                    <td className={`p-3 font-bold ${(r.profit ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      €{(r.profit ?? 0).toFixed(2)}
                    </td>
                  ) : (
                    <td className="p-3 font-bold text-emerald-400">€{(r.importo * r.quota).toFixed(2)}</td>
                  )}
                  <td className="p-3">
                    {r.esito_finale === 'won' ? <span title="Vinta">✅</span>
                      : r.esito_finale === 'lost' ? <span title="Persa">❌</span>
                      : <span title="In gioco">⏳</span>}
                  </td>
                  <td className={`p-3 ${r.ev >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{(r.ev * 100).toFixed(1)}%</td>
                  <td className="p-3"><button onClick={() => removeRow(r.id)} className="text-red-400 hover:text-red-300">✖</button></td>
                </tr>
              ))}
              {cassa.length === 0 && (
                <tr><td colSpan={9} className="p-6 text-center text-gray-500">Cassa vuota. Aggiungi la prima scommessa sopra.</td></tr>
              )}
            </tbody>
            {totali.n > 0 && (
              <tfoot className="bg-gray-900">
                <tr>
                  <td colSpan={4} className="p-3 font-bold text-right">TOTALI</td>
                  <td className="p-3 font-bold">€{totali.speso.toFixed(2)}</td>
                  <td className={`p-3 font-bold ${totali.profit >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    P/L reale €{totali.profit.toFixed(2)} <span className="text-gray-400 font-normal">(ROI {totali.roi.toFixed(1)}%)</span>
                  </td>
                  <td colSpan={3} className="p-3 text-gray-400 text-sm">
                    {totali.chiusi} chiuse · {totali.inGioco} in gioco · vincita pot. in gioco €{totali.vincita.toFixed(2)}
                  </td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </div>
    </main>
  )
}

function ProbBox({ label, value, raw }: { label: string; value: number; raw?: boolean }) {
  return (
    <div className="bg-gray-900 p-3 rounded-lg text-center">
      <div className="text-gray-400 text-sm">{label}</div>
      <div className="text-xl font-bold">{raw ? value.toFixed(2) : `${(value * 100).toFixed(1)}%`}</div>
    </div>
  )
}
