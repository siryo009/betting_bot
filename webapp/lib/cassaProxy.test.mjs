/**
 * Test del proxy Cassa (P2). TUTTI offline: `fetch` e' sempre iniettato e
 * nessuna richiesta di rete viene fatta. Dati finti, nessun segreto reale.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CONFIRM_PHRASE,
  MIN_TOKEN_LEN,
  buildWriteBody,
  forwardCassa,
  timeoutFromEnv,
} from "./cassaProxy.ts";

const BASE = "https://backend.example";
const TOKEN = "test-token-p2-0123456789abcdef";
const ROW = { partita: "Inter vs Napoli", esito: "1", quota: 1.85, importo: 10, ev: 0.03 };

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** fetch finto che registra le chiamate e delega a `impl`. */
function recorder(impl) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return impl(url, init);
  };
  fn.calls = calls;
  return fn;
}

function writeOpts(method, body, extra = {}) {
  return {
    method,
    body,
    backendUrl: BASE,
    adminToken: TOKEN,
    timeoutMs: 600,
    ...extra,
  };
}

test("POST 200: inoltra al backend con Bearer e whitelist dei campi", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true, scommesse: [] }));
  const out = await forwardCassa(writeOpts("POST", { ...ROW, hacked: "x" }, { fetchImpl }));
  assert.equal(out.status, 200);
  assert.equal(out.payload.ok, true);
  const call = fetchImpl.calls[0];
  assert.equal(call.url, `${BASE}/api/cassa`);
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`);
  const sent = JSON.parse(call.init.body);
  assert.deepEqual(sent, ROW);
  assert.equal("hacked" in sent, false);
});

test("POST 401 passa attraverso (token backend rifiutato)", async () => {
  const fetchImpl = recorder(async () => jsonResponse(401, { error: "unauthorized" }));
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl }));
  assert.equal(out.status, 401);
  assert.deepEqual(out.payload, { error: "unauthorized" });
});

test("POST 403 passa attraverso (scritture disattivate dal backend)", async () => {
  const fetchImpl = recorder(async () => jsonResponse(403, { error: "disabled" }));
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl }));
  assert.equal(out.status, 403);
  assert.equal(out.payload.error, "disabled");
});

test("DELETE senza conferma: 428 fail-fast, NESSUNA chiamata al backend", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true }));
  const out = await forwardCassa(writeOpts("DELETE", { confirm: "altro" }, { fetchImpl }));
  assert.equal(out.status, 428);
  assert.equal(out.payload.error, "confirmation_required");
  assert.equal(fetchImpl.calls.length, 0);
});

test("DELETE con conferma: inoltra solo confirm (+ dry_run normalizzato)", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true, eliminate: 2 }));
  const out = await forwardCassa(
    writeOpts("DELETE", { confirm: CONFIRM_PHRASE, dry_run: true, extra: "x" }, { fetchImpl }),
  );
  assert.equal(out.status, 200);
  const sent = JSON.parse(fetchImpl.calls[0].init.body);
  assert.deepEqual(sent, { confirm: CONFIRM_PHRASE, dry_run: 1 });
});

test("DELETE: 428 del backend passa attraverso (non viene mascherato)", async () => {
  const fetchImpl = recorder(async () => jsonResponse(428, { error: "confirmation_required" }));
  const out = await forwardCassa(writeOpts("DELETE", { confirm: CONFIRM_PHRASE }, { fetchImpl }));
  assert.equal(out.status, 428);
  assert.equal(fetchImpl.calls.length, 1);
});

test("errore 500 del backend passa attraverso con il suo corpo", async () => {
  const fetchImpl = recorder(async () => jsonResponse(500, { error: "backup_failed" }));
  const out = await forwardCassa(writeOpts("DELETE", { confirm: CONFIRM_PHRASE }, { fetchImpl }));
  assert.equal(out.status, 500);
  assert.equal(out.payload.error, "backup_failed");
});

test("timeout: 504 e nessuna eccezione verso il chiamante", async () => {
  const fetchImpl = (url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => {
        const err = new Error("aborted");
        err.name = "AbortError";
        reject(err);
      });
    });
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl, timeoutMs: 500 }));
  assert.equal(out.status, 504);
  assert.equal(out.payload.error, "upstream_timeout");
});

test("rete irraggiungibile: 502", async () => {
  const fetchImpl = async () => {
    throw new TypeError("fetch failed");
  };
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl }));
  assert.equal(out.status, 502);
  assert.equal(out.payload.error, "upstream_unreachable");
});

test("risposta non JSON (HTML): 502, nessun contenuto rimbalzato", async () => {
  const fetchImpl = recorder(
    async () => new Response("<html>bad gateway</html>", { status: 200 }),
  );
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl }));
  assert.equal(out.status, 502);
  assert.equal(out.payload.error, "upstream_invalid_response");
  assert.equal(JSON.stringify(out.payload).includes("<html>"), false);
});

test("risposta JSON non oggetto (array): 502", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, []));
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl }));
  assert.equal(out.status, 502);
});

test("token assente: 503 fail-closed, nessuna chiamata", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, {}));
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl, adminToken: "" }));
  assert.equal(out.status, 503);
  assert.equal(out.payload.error, "proxy_disabled");
  assert.equal(fetchImpl.calls.length, 0);
});

test("token troppo corto: 503 fail-closed", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, {}));
  const short = "x".repeat(MIN_TOKEN_LEN - 1);
  const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl, adminToken: short }));
  assert.equal(out.status, 503);
  assert.equal(out.payload.error, "proxy_token_too_short");
  assert.equal(fetchImpl.calls.length, 0);
});

test("BACKEND_URL assente o non http: 503 fail-closed", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, {}));
  for (const backendUrl of ["", "ftp://x", "not-a-url", null]) {
    const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl, backendUrl }));
    assert.equal(out.status, 503);
    assert.equal(out.payload.error, "proxy_not_configured");
  }
  assert.equal(fetchImpl.calls.length, 0);
});

test("GET: nessun token, nessun body (lettura pubblica pass-through)", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { scommesse: [] }));
  const out = await forwardCassa({
    method: "GET",
    backendUrl: BASE,
    adminToken: null,
    fetchImpl,
  });
  assert.equal(out.status, 200);
  const call = fetchImpl.calls[0];
  assert.equal(call.init.headers.Authorization, undefined);
  assert.equal(call.init.body, undefined);
});

test("POST non valido: 400 e nessuna chiamata", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, {}));
  const invalid = [
    { ...ROW, quota: 1 },
    { ...ROW, quota: "abc" },
    { ...ROW, importo: 0 },
    { ...ROW, partita: "  " },
    { ...ROW, esito: "" },
    { ...ROW, ev: "nope" },
    null,
    [],
  ];
  for (const body of invalid) {
    const out = await forwardCassa(writeOpts("POST", body, { fetchImpl }));
    assert.equal(out.status, 400);
    assert.equal(out.payload.error, "invalid_body");
  }
  assert.equal(fetchImpl.calls.length, 0);
});

test("data non ISO viene scartata (whitelist, non pass-through)", () => {
  const ok = buildWriteBody("POST", { ...ROW, data: "19/10/2026" });
  assert.equal(ok.ok, true);
  assert.equal("data" in ok.body, false);
  const good = buildWriteBody("POST", { ...ROW, data: "2026-10-19" });
  assert.equal(good.ok, true);
  assert.equal(good.body.data, "2026-10-19");
});

test("il token non compare mai nella risposta del proxy", async () => {
  const cases = [
    async () => jsonResponse(200, { ok: true }),
    async () => jsonResponse(401, { error: "unauthorized" }),
    async () => new Response("<html>", { status: 500 }),
  ];
  for (const impl of cases) {
    const out = await forwardCassa(writeOpts("POST", ROW, { fetchImpl: recorder(impl) }));
    assert.equal(JSON.stringify(out).includes(TOKEN), false);
  }
});

test("timeoutFromEnv: default su valori impossibili, clamp sui limiti", () => {
  assert.equal(timeoutFromEnv(undefined), 8000);
  assert.equal(timeoutFromEnv(null), 8000);
  assert.equal(timeoutFromEnv(""), 8000);
  assert.equal(timeoutFromEnv("abc"), 8000);
  assert.equal(timeoutFromEnv("0"), 8000);
  assert.equal(timeoutFromEnv("-5"), 8000);
  assert.equal(timeoutFromEnv("100"), 500);
  assert.equal(timeoutFromEnv("3000"), 3000);
  assert.equal(timeoutFromEnv("999999"), 30000);
});
