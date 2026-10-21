/**
 * Test del client Cassa (P2). TUTTI offline: `fetch` e' iniettato.
 * Coprono i casi richiesti: 200, 401, 403, 428, 500, timeout, rete, non-JSON.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { CASSA_ENDPOINT, deleteCassa, loadState, postCassa, readCassaError } from "./cassaClient.ts";

const ROW = { partita: "Inter vs Napoli", esito: "1", quota: 1.85, importo: 10, ev: 0.03 };
const TOKEN = "test-token-p2-0123456789abcdef";

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function recorder(impl) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return impl(url, init);
  };
  fn.calls = calls;
  return fn;
}

test("POST 200: esito ok e body corretto verso l'endpoint same-origin", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true }));
  const res = await postCassa(ROW, fetchImpl);
  assert.deepEqual(res, { ok: true, status: 200, message: "" });
  const call = fetchImpl.calls[0];
  assert.equal(call.url, CASSA_ENDPOINT);
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.credentials, undefined);
  assert.equal(JSON.parse(call.init.body).partita, ROW.partita);
});

test("POST 401: non ok, status e messaggio del backend", async () => {
  const fetchImpl = recorder(async () =>
    jsonResponse(401, { error: "unauthorized", message: "Token errato." }),
  );
  const res = await postCassa(ROW, fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 401);
  assert.equal(res.message, "Token errato.");
});

test("POST 403: scritture disattivate -> non ok", async () => {
  const fetchImpl = recorder(async () =>
    jsonResponse(403, { error: "disabled", message: "Scritture disattivate: imposta API_WRITE_ENABLED=1." }),
  );
  const res = await postCassa(ROW, fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 403);
  assert.match(res.message, /API_WRITE_ENABLED/);
});

test("DELETE 428: conferma rifiutata -> non ok", async () => {
  const fetchImpl = recorder(async () =>
    jsonResponse(428, { error: "confirmation_required", message: "Per svuotare la cassa invia confirm=CANCELLA_CASSA." }),
  );
  const res = await deleteCassa("sbagliata", fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 428);
  assert.match(res.message, /CANCELLA_CASSA/);
});

test("DELETE 200: la conferma viaggia nel body, mai in query string", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true, eliminate: 3 }));
  const res = await deleteCassa("CANCELLA_CASSA", fetchImpl);
  assert.equal(res.ok, true);
  const call = fetchImpl.calls[0];
  assert.equal(call.url, CASSA_ENDPOINT);
  assert.equal(call.url.includes("?"), false);
  assert.deepEqual(JSON.parse(call.init.body), { confirm: "CANCELLA_CASSA" });
});

test("POST 500 con corpo JSON: messaggio dal backend", async () => {
  const fetchImpl = recorder(async () =>
    jsonResponse(500, { error: "backup_failed", message: "Backup non scritto: cancellazione annullata." }),
  );
  const res = await postCassa(ROW, fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 500);
  assert.match(res.message, /cancellazione annullata/);
});

test("500 con HTML: nessuna eccezione, messaggio = HTTP 500", async () => {
  const fetchImpl = recorder(async () => new Response("<html>oops</html>", { status: 500 }));
  const res = await postCassa(ROW, fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 500);
  assert.equal(res.message, "HTTP 500");
});

test("rete: status 0 e messaggio esplicito", async () => {
  const fetchImpl = async () => {
    throw new TypeError("Failed to fetch");
  };
  const res = await postCassa(ROW, fetchImpl);
  assert.equal(res.ok, false);
  assert.equal(res.status, 0);
  assert.equal(res.message, "Server non raggiungibile");
});

test("timeout (AbortError): status 0 e messaggio di timeout", async () => {
  const fetchImpl = async () => {
    const err = new Error("aborted");
    err.name = "AbortError";
    throw err;
  };
  const res = await deleteCassa("CANCELLA_CASSA", fetchImpl, 500);
  assert.equal(res.ok, false);
  assert.equal(res.status, 0);
  assert.match(res.message, /Timeout/);
});

test("readCassaError: JSON senza message usa error, non-JSON usa lo status", async () => {
  assert.equal(await readCassaError(jsonResponse(422, { error: "invalid_body" })), "invalid_body");
  assert.equal(await readCassaError(new Response("plain", { status: 502 })), "HTTP 502");
});

test("loadState: solo il server confermato con righe vale 'ok'", () => {
  assert.equal(loadState({ serverOk: true, serverRows: 3, localRows: 0 }), "ok");
  assert.equal(loadState({ serverOk: true, serverRows: 3, localRows: 5 }), "ok");
  assert.equal(loadState({ serverOk: true, serverRows: 0, localRows: 0 }), "ok");
});

test("loadState: copia locale senza conferma NON e' 'ok'", () => {
  // Server raggiunto ma vuoto: le righe locali non sono sul server.
  assert.equal(loadState({ serverOk: true, serverRows: 0, localRows: 2 }), "locale");
  // Errore HTTP con copia locale.
  assert.equal(loadState({ serverOk: false, serverRows: 0, localRows: 2 }), "locale");
  // Rete assente con copia locale.
  assert.equal(
    loadState({ serverOk: false, serverRows: 0, localRows: 2, networkError: true }),
    "locale",
  );
});

test("loadState: senza copia locale distingue errore HTTP da rete", () => {
  assert.equal(loadState({ serverOk: false, serverRows: 0, localRows: 0 }), "errore");
  assert.equal(
    loadState({ serverOk: false, serverRows: 0, localRows: 0, networkError: true }),
    "offline",
  );
});

test("il token non viene mai inviato dal browser", async () => {
  const fetchImpl = recorder(async () => jsonResponse(200, { ok: true }));
  await postCassa(ROW, fetchImpl);
  const call = fetchImpl.calls[0];
  assert.equal(JSON.stringify(call.init.headers).includes(TOKEN), false);
  assert.equal(call.init.headers.Authorization, undefined);
});
