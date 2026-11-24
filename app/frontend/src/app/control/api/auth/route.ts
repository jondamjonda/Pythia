// Control-room unlock (UI Build Plan §2.4). POST {token} → if it matches CONTROL_PANEL_TOKEN (constant-
// time compare), set an httpOnly cookie so the proxy gate opens. DELETE → lock again. The raw secret is
// only ever compared server-side here; it is never sent to the browser. Next 16: `cookies()` is async.
import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

const AUTH_COOKIE = "pc_auth";

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function POST(request: NextRequest): Promise<Response> {
  const configured = process.env.CONTROL_PANEL_TOKEN ?? "";
  let token = "";
  try {
    token = ((await request.json()) as { token?: string }).token ?? "";
  } catch {
    token = "";
  }
  // Local-first: no token configured → always unlocked.
  if (configured && !timingSafeEqual(token, configured)) {
    return Response.json({ ok: false, error: "incorrect token" }, { status: 401 });
  }
  const store = await cookies();
  store.set(AUTH_COOKIE, "1", {
    httpOnly: true,
    sameSite: "lax",
    path: "/control",
    maxAge: 60 * 60 * 12, // 12h operator session
  });
  return Response.json({ ok: true });
}

export async function DELETE(): Promise<Response> {
  const store = await cookies();
  store.delete(AUTH_COOKIE);
  return Response.json({ ok: true });
}
