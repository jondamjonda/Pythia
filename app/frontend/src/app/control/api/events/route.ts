// Control-room SSE proxy (UI Build Plan §0.2/§2.4). Streams the FastAPI __system__ channel
// (/admin/api/events) through the Next origin, attaching the token server-side. The browser opens
// exactly one EventSource at /control/api/events; it never connects to :8002 directly.
import type { NextRequest } from "next/server";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8002";
const TOKEN = process.env.CONTROL_PANEL_TOKEN ?? "";

const SSE_HEADERS = {
  "content-type": "text/event-stream",
  "cache-control": "no-cache, no-transform",
  connection: "keep-alive",
};

export async function GET(_request: NextRequest): Promise<Response> {
  const h = new Headers({ accept: "text/event-stream" });
  if (TOKEN) h.set("X-Control-Token", TOKEN);
  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/admin/api/events`, { headers: h, cache: "no-store" });
  } catch (e) {
    return new Response(`event: error\ndata: ${JSON.stringify({ error: String(e) })}\n\n`, {
      status: 502,
      headers: SSE_HEADERS,
    });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response(`event: error\ndata: backend ${upstream.status}\n\n`, {
      status: 502,
      headers: SSE_HEADERS,
    });
  }
  return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
}

export const dynamic = "force-dynamic";
