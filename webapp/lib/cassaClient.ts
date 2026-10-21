/**
 * Client della Cassa (P2, 19/10/2026) — lato browser.
 *
 * Parla SOLO con l'endpoint same-origin `/api/cassa` (proxy Next): cosi' non
 * esiste preflight CORS e il token admin resta sul server. Ogni esito e'
 * esplicito: `ok === false` significa che il server NON ha fatto l'operazione,
 * e la UI non deve dire il contrario.
 */

export const CASSA_ENDPOINT = "/api/cassa";
export const DEFAULT_TIMEOUT_MS = 15000;

export type CassaResult = { ok: boolean; status: number; message: string };
/**
 * Esito DICHIARATO di una lettura. `locale` esiste perche' una copia in
 * localStorage non e' una sincronizzazione: mostrarla come `ok` direbbe il
 * falso (P2: prima la pagina usciva in early-return con `ok` senza mai
 * contattare il server).
 */
export type CassaLoadState = "ok" | "locale" | "errore" | "offline";

export function loadState(opts: {
  serverOk: boolean;
  serverRows: number;
  localRows: number;
  networkError?: boolean;
}): CassaLoadState {
  if (opts.serverOk) {
    if (opts.serverRows > 0) return "ok";
    // Server raggiunto ma vuoto: le righe locali non sono confermate da lui.
    return opts.localRows > 0 ? "locale" : "ok";
  }
  if (opts.networkError) return opts.localRows > 0 ? "locale" : "offline";
  return opts.localRows > 0 ? "locale" : "errore";
}
export type CassaWritePayload = {
  partita: string;
  esito: string;
  quota: number;
  importo: number;
  ev?: number;
  data?: string;
};
type FetchLike = typeof fetch;

/** Messaggio d'errore leggibile; una risposta HTML/plain non fa esplodere nulla. */
export async function readCassaError(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const rec = data as Record<string, unknown>;
      if (typeof rec.message === "string" && rec.message) return rec.message;
      if (typeof rec.error === "string" && rec.error) return rec.error;
    }
  } catch {
    /* risposta non JSON: sotto si usa lo status */
  }
  return `HTTP ${res.status}`;
}

async function call(
  method: "POST" | "DELETE",
  payload: unknown,
  fetchImpl: FetchLike,
  timeoutMs: number,
): Promise<CassaResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(CASSA_ENDPOINT, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res.ok) {
      return { ok: false, status: res.status, message: await readCassaError(res) };
    }
    return { ok: true, status: res.status, message: "" };
  } catch (err) {
    const name = (err as { name?: string } | null)?.name;
    const timedOut = name === "AbortError" || name === "TimeoutError";
    return {
      ok: false,
      status: 0,
      message: timedOut ? "Timeout o connessione interrotta" : "Server non raggiungibile",
    };
  } finally {
    clearTimeout(timer);
  }
}

export function postCassa(
  row: CassaWritePayload,
  fetchImpl: FetchLike = fetch,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<CassaResult> {
  return call(
    "POST",
    {
      partita: row.partita,
      esito: row.esito,
      quota: row.quota,
      importo: row.importo,
      ev: row.ev ?? 0,
      data: row.data,
    },
    fetchImpl,
    timeoutMs,
  );
}

/** DELETE con conferma esplicita: la frase viaggia nel body, mai in query string. */
export function deleteCassa(
  confirmPhrase: string,
  fetchImpl: FetchLike = fetch,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<CassaResult> {
  return call("DELETE", { confirm: confirmPhrase }, fetchImpl, timeoutMs);
}
