// Control-room gate (UI Build Plan §2.4). In Next 16 this file is `proxy.ts` (was `middleware.ts`).
// It is an OPTIMISTIC permission check only — the real enforcement is server-side: the /control/api/*
// route handlers forward CONTROL_PANEL_TOKEN to FastAPI, which validates it. Here we just bounce a
// browser that hasn't unlocked to the unlock page, so /control/* is never shown un-gated.
//
// Local-first: if CONTROL_PANEL_TOKEN is unset AND the environment is local, there is no secret to
// check → the gate is open and the proxy forwards no token (the loopback bind is the boundary).
//
// Fail-closed mirror (WP-026 / A4-05b — the FE half of the backend's environment guard): if the token
// is UNCONFIGURED but the environment signal is prod/staging, we must NOT silently open the gate. That
// would expose the control surface in a deployed env where loopback is no longer the boundary. Instead
// we refuse to forward and render a misconfiguration banner, matching the backend's 503 semantics. The
// env signal mirrors the backend `settings.environment` ("local"|"staging"|"prod") — read here from the
// server-only `ENVIRONMENT` var (no NEXT_PUBLIC_ prefix → never reaches the browser), defaulting to
// "local" exactly like the backend default.
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const AUTH_COOKIE = "pc_auth";
// Paths reachable WITHOUT the cookie, so the operator can actually unlock.
const OPEN = ["/control/unlock", "/control/api/auth"];

// Mirror of backend `settings.environment` (config.py A1-T09). Server-only; default "local".
function environment(): string {
  return process.env.ENVIRONMENT ?? "local";
}

// A non-local deploy must have a control token configured; loopback is no longer the boundary.
function isDeployed(): boolean {
  return environment() !== "local";
}

// Misconfiguration response. Matches the backend 503 (Service Unavailable) semantics: the control
// surface is refused, NOT redirected, because there is no safe unlock path when the secret is unset in
// a deployed env. We render a small operator-facing banner rather than silently forwarding.
function misconfiguredBanner(): NextResponse {
  const body =
    "Control room misconfigured: CONTROL_PANEL_TOKEN is unset but ENVIRONMENT is " +
    `"${environment()}" (not local). Refusing to expose /control/* without a token. ` +
    "Set CONTROL_PANEL_TOKEN to unlock, or run with ENVIRONMENT=local for loopback-only access.";
  return new NextResponse(body, {
    status: 503,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;
  const tokenConfigured = Boolean(process.env.CONTROL_PANEL_TOKEN);

  if (!tokenConfigured) {
    // Fail-closed in a deployed env (mirror of backend WP-026): refuse instead of opening the gate.
    if (isDeployed()) return misconfiguredBanner();
    return NextResponse.next(); // local + no secret → local-first, gate open (loopback is the boundary)
  }
  if (OPEN.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return NextResponse.next();
  }
  if (request.cookies.get(AUTH_COOKIE)?.value) return NextResponse.next();

  const url = request.nextUrl.clone();
  url.pathname = "/control/unlock";
  url.searchParams.set("next", pathname);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: "/control/:path*",
};
