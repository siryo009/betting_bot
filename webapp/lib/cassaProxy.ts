/**
 * Proxy SERVER-SIDE della Cassa (P2, 19/10/2026).
 *
 * Perche' esiste: dopo P1 le scritture della Cassa richiedono il token admin e
 * `API_WRITE_ENABLED`; il browser non puo' (e non deve) possedere quel token.
 * Questa funzione e' il ponte: gira SOLO sul server Next, legge il token da una
 * variabile server-side e inoltra la richiesta al backend Python.
 *
 * Regole non negoziabili:
 * - il token non viene mai restituito, loggato o messo in un payload d'errore;
 * - `API_WRITE_ENABLED` NON viene impostato ne' aggirato: il backend resta
 *   l'autorita' finale (403 se le scritture sono spente);
 * - la conferma `CANCELLA_CASSA` e' verificata anche qui (fail-fast 428), ma il
 *   backend la riverifica: nessun bypass;
 * - i campi inoltrati sono una whitelist (niente pass-through arbitrario);
 * - ogni esito e' JSON, anche quando il backend risponde HTML o non risponde.
 *
 * Nessun log e nessun accesso a rete qui: `fetchImpl` e' iniettabile, cosi' i
 * test girano offline.
 */

export const CONFIRM_PHRASE = "CANCELLA_CASSA";
export const DEFAULT_TIMEOUT_MS = 8000;
export const MAX_TIMEOUT_MS = 30000;
export const MIN_TOKEN_LEN = 24;
const MAX_UPSTREAM_CHARS = 2_000_000;

export type ProxyResult = { status: number; payload: Record<string, unknown> };

type BodyOk = { ok: true; body: Record<string, unknown> };
type BodyFail = { ok: false; result: ProxyResult };
export type BodyCheck = BodyOk | BodyFail;

function fail(status: number, error: string, message: string): ProxyResult {
  return { status, payload: { error, message } };
}

/** Timeout da env: valori impossibili NON disattivano la guardia (default). */
export function timeoutFromEnv(raw?: string | null): number {
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_TIMEOUT_MS;
  return Math.min(Math.max(Math.floor(n), 500), MAX_TIMEOUT_MS);
}

function asRecord(raw: unknown): Record<string, unknown> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return raw as Record<string, unknown>;
}

/**
 * Valida e normalizza il corpo di una scrittura. NON tocca la rete.
 *
 * POST: solo i campi che il backend conosce (partita, esito, quota, importo,
 * ev, data) e con gli stessi vincoli minimi (`quota > 1`, `importo > 0`).
 * DELETE: solo `confirm` (+ `dry_run`); senza la frase esatta -> 428.
 */
export function buildWriteBody(method: "POST" | "DELETE", raw: unknown): BodyCheck {
  const obj = asRecord(raw);
  if (!obj) {
    return { ok: false, result: fail(400, "invalid_body", "Corpo della richiesta non valido.") };
  }
  if (method === "DELETE") {
    const confirm = typeof obj.confirm === "string" ? obj.confirm.trim() : "";
    if (confirm !== CONFIRM_PHRASE) {
      return {
        ok: false,
        result: fail(
          428,
          "confirmation_required",
          `Per svuotare la cassa invia confirm=${CONFIRM_PHRASE}.`,
        ),
      };
    }
    const body: Record<string, unknown> = { confirm: CONFIRM_PHRASE };
    const dry = obj.dry_run;
    if (dry === true || dry === 1 || dry === "1" || dry === "true") {
      body.dry_run = 1;
    }
    return { ok: true, body };
  }

  const partita = typeof obj.partita === "string" ? obj.partita.trim() : "";
  const esito = typeof obj.esito === "string" ? obj.esito.trim() : "";
  const quota = Number(obj.quota);
  const importo = Number(obj.importo);
  const ev = obj.ev == null ? 0 : Number(obj.ev);
  if (!partita || !esito) {
    return { ok: false, result: fail(400, "invalid_body", "Servono partita ed esito.") };
  }
  if (!Number.isFinite(quota) || quota <= 1) {
    return { ok: false, result: fail(400, "invalid_body", "Quota non valida (deve essere > 1).") };
  }
  if (!Number.isFinite(importo) || importo <= 0) {
    return { ok: false, result: fail(400, "invalid_body", "Importo non valido (deve essere > 0).") };
  }
  if (!Number.isFinite(ev)) {
    return { ok: false, result: fail(400, "invalid_body", "EV non numerico.") };
  }
  const body: Record<string, unknown> = { partita, esito, quota, importo, ev };
  if (typeof obj.data === "string" && /^\d{4}-\d{2}-\d{2}$/.test(obj.data)) {
    body.data = obj.data;
  }
  return { ok: true, body };
}

export type ForwardOptions = {
  method: "GET" | "POST" | "DELETE";
  body?: unknown;
  backendUrl?: string | null;
  adminToken?: string | null;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
};

/**
 * Inoltra una richiesta a `${BACKEND_URL}/api/cassa`.
 *
 * Il token viene inviato SOLO per POST/DELETE. Fail-closed: senza
 * `BACKEND_URL` o senza un token server-side adeguato la scrittura risponde
 * 503 e la rete non viene nemmeno contattata.
 */
export async function forwardCassa(opts: ForwardOptions): Promise<ProxyResult> {
  const backend = (opts.backendUrl || "").trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(backend)) {
    return fail(503, "proxy_not_configured", "BACKEND_URL non configurato: proxy disattivato.");
  }

  let payload: Record<string, unknown> | undefined;
  let token = "";
  if (opts.method !== "GET") {
    const check = buildWriteBody(opts.method, opts.body);
    if (!check.ok) return check.result;
    payload = check.body;
    token = (opts.adminToken || "").trim();
    if (!token) {
      return fail(
        503,
        "proxy_disabled",
        "BACKEND_ADMIN_TOKEN non configurato: scritture Cassa disattivate (fail-closed).",
      );
    }
    if (token.length < MIN_TOKEN_LEN) {
      return fail(503, "proxy_token_too_short", "BACKEND_ADMIN_TOKEN troppo corto: scritture disattivate.");
    }
  }

  const timeoutMs = Math.min(
    Math.max(Math.floor(opts.timeoutMs ?? DEFAULT_TIMEOUT_MS), 500),
    MAX_TIMEOUT_MS,
  );
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers.Authorization = `Bearer ${token}`;
  }

  let res: Response;
  try {
    res = await (opts.fetchImpl ?? fetch)(`${backend}/api/cassa`, {
      method: opts.method,
      headers,
      body: opts.method === "GET" ? undefined : JSON.stringify(payload),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (err) {
    const name = (err as { name?: string } | null)?.name;
    const timedOut = controller.signal.aborted || name === "AbortError" || name === "TimeoutError";
    return timedOut
      ? fail(504, "upstream_timeout", "Il backend non ha risposto entro il timeout.")
      : fail(502, "upstream_unreachable", "Backend non raggiungibile.");
  } finally {
    clearTimeout(timer);
  }

  let text: string;
  try {
    text = await res.text();
  } catch {
    return fail(502, "upstream_unreachable", "Risposta del backend non leggibile.");
  }
  if (text.length > MAX_UPSTREAM_CHARS) {
    return fail(502, "upstream_too_large", "Risposta del backend troppo grande.");
  }
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    return fail(502, "upstream_invalid_response", "Il backend ha risposto in un formato non JSON.");
  }
  const payloadOut = asRecord(parsed);
  if (!payloadOut) {
    return fail(502, "upstream_invalid_response", "Il backend ha risposto in un formato non atteso.");
  }
  return { status: res.status, payload: payloadOut };
}
