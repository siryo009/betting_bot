import { NextResponse } from "next/server";
import { forwardCassa, timeoutFromEnv } from "../../../lib/cassaProxy";

/**
 * Endpoint same-origin della Cassa (P2, 19/10/2026).
 *
 * - GET   : pass-through pubblico (la pagina continua a leggere la cassa);
 * - POST  : scrive una riga, con il token admin letto SOLO da env server-side;
 * - DELETE: svuota la cassa, richiede `confirm=CANCELLA_CASSA` (verificata dal
 *           proxy E dal backend, che crea anche il backup atomico).
 *
 * `BACKEND_ADMIN_TOKEN` non e' mai serializzato ne' loggato. Se manca, il proxy
 * risponde 503: nessuna scrittura, nessun fallback silenzioso.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function run(method: "GET" | "POST" | "DELETE", req: Request) {
  let body: unknown;
  if (method !== "GET") {
    let text: string;
    try {
      text = await req.text();
    } catch {
      return NextResponse.json(
        { error: "invalid_body", message: "Corpo della richiesta non leggibile." },
        { status: 400, headers: { "Cache-Control": "no-store" } },
      );
    }
    if (!text) {
      body = {};
    } else {
      try {
        body = JSON.parse(text);
      } catch {
        return NextResponse.json(
          { error: "invalid_json", message: "Corpo della richiesta non JSON." },
          { status: 400, headers: { "Cache-Control": "no-store" } },
        );
      }
    }
  }
  const out = await forwardCassa({
    method,
    body,
    // BACKEND_URL e' la variabile server-side prevista; NEXT_PUBLIC_API_BASE
    // resta solo come ripiego di disponibilita' (un URL non e' un segreto).
    backendUrl: process.env.BACKEND_URL || process.env.NEXT_PUBLIC_API_BASE,
    adminToken: process.env.BACKEND_ADMIN_TOKEN,
    timeoutMs: timeoutFromEnv(process.env.CASSA_PROXY_TIMEOUT_MS),
  });
  return NextResponse.json(out.payload, {
    status: out.status,
    headers: { "Cache-Control": "no-store" },
  });
}

export async function GET(req: Request) {
  return run("GET", req);
}

export async function POST(req: Request) {
  return run("POST", req);
}

export async function DELETE(req: Request) {
  return run("DELETE", req);
}
