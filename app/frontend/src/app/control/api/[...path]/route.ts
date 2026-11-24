// Control-room data proxy (UI Build Plan §2.4/P3-4). The browser only ever talks to the same Next
// origin; this server-side handler forwards /control/api/* → FastAPI /admin/api/*, attaching the
// X-Control-Token so the secret never reaches the client. The more-specific /control/api/{auth,events}
// routes take precedence over this catch-all. Next 16: route `params` is a Promise (await it).
//
// Fail-closed mirror (WP-026 / A4-05b): the `?? ""` empty-token default below must NOT be forwarded as
// an authenticated request in a deployed env. Locally an empty token is fine (loopback is the boundary;
// FastAPI is also token-less). But in prod/staging an unconfigured token means the control surface has
// no real auth — so we refuse with 503 (matching the backend's misconfiguration semantics) rather than
// forwarding a request with no X-Control-Token header. The env signal mirrors `settings.environment`.
import type { NextRequest } from "next/server";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8002";
const TOKEN = process.env.CONTROL_PANEL_TOKEN ?? "";
// Mirror of backend `settings.environment` (config.py A1-T09). Server-only; default "local".
const ENVIRONMENT = process.env.ENVIRONMENT ?? "local";
const IS_DEPLOYED = ENVIRONMENT !== "local";

function backendHeaders(contentType: string | null): Headers {
  const h = new Headers();
  if (TOKEN) h.set("X-Control-Token", TOKEN);
  if (contentType) h.set("content-type", contentType);
  return h;
}

// 503 refusal matching the backend misconfiguration semantics: an empty token in a deployed env is NOT
// forwarded as authenticated. Local (loopback) pass-through is preserved.
function misconfigured(): Response {
  return Response.json(
    {
      error: "control room misconfigured",
      detail:
        `CONTROL_PANEL_TOKEN is unset but ENVIRONMENT is "${ENVIRONMENT}" (not local); ` +
        "refusing to forward an unauthenticated control request.",
    },
    { status: 503 },
  );
}

async function forward(request: NextRequest, path: string[], method: string): Promise<Response> {
  // Fail-closed: never forward an empty token as an authenticated request in a deployed env.
  if (!TOKEN && IS_DEPLOYED) return misconfigured();
  const target = `${BACKEND}/admin/api/${path.join("/")}${request.nextUrl.search}`;
  const init: RequestInit = {
    method,
    headers: backendHeaders(request.headers.get("content-type")),
  };
  if (method !== "GET" && method !== "HEAD") init.body = await request.text();
  try {
    const resp = await fetch(target, init);
    const body = await resp.text();
    return new Response(body, {
      status: resp.status,
      headers: { "content-type": resp.headers.get("content-type") ?? "application/json" },
    });
  } catch (e) {
    return Response.json({ error: "backend unreachable", detail: String(e) }, { status: 502 });
  }
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, ctx: Ctx): Promise<Response> {
  return forward(request, (await ctx.params).path, "GET");
}
export async function POST(request: NextRequest, ctx: Ctx): Promise<Response> {
  return forward(request, (await ctx.params).path, "POST");
}
export const dynamic = "force-dynamic";
