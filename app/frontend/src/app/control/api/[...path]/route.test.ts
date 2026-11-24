// Co-located FE test for the admin-proxy fail-closed mirror (WP-026 / A4-05b — the FE half).
//
// What this guards (mirror of the backend's environment-driven 503 misconfiguration semantics):
//   1. data-proxy route: an UNCONFIGURED (empty) control token in a DEPLOYED env (prod/staging) must NOT
//      be forwarded as an authenticated request — the handler refuses with 503 (no fetch attempted).
//   2. LOCAL pass-through is preserved: empty token + ENVIRONMENT=local still forwards to the backend.
//   3. proxy.ts gate: the same env predicate — when the token is unset AND the env is prod/staging the
//      gate refuses (503 banner) instead of returning next(); LOCAL stays open (pass-through).
//
// Runner: this uses only the Node built-in test runner + assert (no extra devDependency). Node 22 strips
// TypeScript types natively, so the orchestrator runs it with:
//     node --test --experimental-strip-types "src/app/control/api/[...path]/route.test.ts"
// (run from the `frontend/` dir so `next` and the relative route module resolve). The route handler
// imports only a *type* from next/server, which type-stripping erases, so it loads under bare Node. The
// module reads its env vars at load time, so each case sets env BEFORE a cache-busting dynamic import.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Minimal NextRequest stand-in — the handler only touches nextUrl.search, headers, and text().
function makeReq(): unknown {
  return { nextUrl: { search: "" }, headers: new Headers(), text: async () => "" };
}
function makeCtx(path: string[]): unknown {
  return { params: Promise.resolve({ path }) };
}

// Load the data-proxy route module fresh for a given env (cache-bust via query string, because the
// module captures TOKEN/ENVIRONMENT into top-level consts at evaluation time).
async function loadRoute(
  env: { ENVIRONMENT?: string; CONTROL_PANEL_TOKEN?: string; BACKEND_URL?: string },
  tag: string,
): Promise<{ GET: (req: unknown, ctx: unknown) => Promise<Response> }> {
  process.env.ENVIRONMENT = env.ENVIRONMENT ?? "local";
  process.env.CONTROL_PANEL_TOKEN = env.CONTROL_PANEL_TOKEN ?? "";
  process.env.BACKEND_URL = env.BACKEND_URL ?? "http://127.0.0.1:1";
  return (await import(`./route.ts?case=${tag}`)) as {
    GET: (req: unknown, ctx: unknown) => Promise<Response>;
  };
}

// ── data-proxy route handler ──────────────────────────────────────────────────────────────────────

test("route: prod + empty token → 503 misconfigured, request NOT forwarded as authed", async () => {
  // BACKEND_URL points somewhere that WOULD answer if we forwarded; the 503 proves we refused first.
  const mod = await loadRoute(
    { ENVIRONMENT: "prod", CONTROL_PANEL_TOKEN: "", BACKEND_URL: "http://127.0.0.1:8002" },
    "prod-empty",
  );
  const resp = await mod.GET(makeReq(), makeCtx(["system"]));
  assert.equal(resp.status, 503, "deployed + unconfigured token must refuse, not forward");
  const body = (await resp.json()) as { error: string };
  assert.match(body.error, /misconfigured/i);
});

test("route: staging + empty token → 503 misconfigured (prod is not the only deployed env)", async () => {
  const mod = await loadRoute(
    { ENVIRONMENT: "staging", CONTROL_PANEL_TOKEN: "", BACKEND_URL: "http://127.0.0.1:8002" },
    "staging-empty",
  );
  const resp = await mod.GET(makeReq(), makeCtx(["system"]));
  assert.equal(resp.status, 503);
});

test("route: LOCAL + empty token → pass-through preserved (forwards; 502 only because backend down)", async () => {
  // ENVIRONMENT=local must NOT refuse. We point BACKEND at a dead port so the forward THROWS and we land
  // on the existing 502 'backend unreachable' branch — which proves a forward was ATTEMPTED, not a 503.
  const mod = await loadRoute(
    { ENVIRONMENT: "local", CONTROL_PANEL_TOKEN: "", BACKEND_URL: "http://127.0.0.1:1" },
    "local-empty",
  );
  const resp = await mod.GET(makeReq(), makeCtx(["system"]));
  assert.equal(resp.status, 502, "local empty-token must forward, not 503-refuse");
  const body = (await resp.json()) as { error: string };
  assert.equal(body.error, "backend unreachable");
});

test("route: prod + CONFIGURED token → forwards (with header); 502 only because backend down", async () => {
  // A configured token in prod is the happy deployed path: it forwards (X-Control-Token set), so a dead
  // backend yields 502, never the 503 refusal.
  const mod = await loadRoute(
    { ENVIRONMENT: "prod", CONTROL_PANEL_TOKEN: "s3cret", BACKEND_URL: "http://127.0.0.1:1" },
    "prod-configured",
  );
  const resp = await mod.GET(makeReq(), makeCtx(["system"]));
  assert.equal(resp.status, 502, "configured token in prod must forward, not 503-refuse");
});

// ── proxy.ts gate (the Next-16-renamed middleware) ──────────────────────────────────────────────────
//
// proxy.ts imports the `NextResponse` *runtime* from next/server, which does not resolve under bare Node
// ESM (it needs Next's bundler conditions). So we (a) assert the gate's decision predicate directly with
// the identical boolean logic the source uses, and (b) assert the source actually wires the fail-closed
// 503 branch (guarding against a regression that re-opens the gate). The predicate mirrors proxy.ts:
//   tokenConfigured = Boolean(CONTROL_PANEL_TOKEN); isDeployed = (ENVIRONMENT ?? "local") !== "local".
//   !tokenConfigured && isDeployed  → REFUSE (503 banner)
//   !tokenConfigured && !isDeployed → next()  (local-first, gate open)

function gateDecision(env: { ENVIRONMENT?: string; CONTROL_PANEL_TOKEN?: string }): "refuse" | "open" {
  const tokenConfigured = Boolean(env.CONTROL_PANEL_TOKEN);
  const isDeployed = (env.ENVIRONMENT ?? "local") !== "local";
  if (!tokenConfigured && isDeployed) return "refuse";
  return "open"; // (either token configured → cookie/redirect flow, or local-first open)
}

test("proxy gate: unconfigured token + prod → refuse (not next())", () => {
  assert.equal(gateDecision({ ENVIRONMENT: "prod", CONTROL_PANEL_TOKEN: "" }), "refuse");
});

test("proxy gate: unconfigured token + staging → refuse", () => {
  assert.equal(gateDecision({ ENVIRONMENT: "staging", CONTROL_PANEL_TOKEN: "" }), "refuse");
});

test("proxy gate: unconfigured token + local → open (pass-through preserved)", () => {
  assert.equal(gateDecision({ ENVIRONMENT: "local", CONTROL_PANEL_TOKEN: "" }), "open");
  assert.equal(gateDecision({ CONTROL_PANEL_TOKEN: "" }), "open"); // env unset defaults to local
});

test("proxy gate: configured token + prod → not the fail-closed branch (normal cookie/redirect flow)", () => {
  assert.equal(gateDecision({ ENVIRONMENT: "prod", CONTROL_PANEL_TOKEN: "s3cret" }), "open");
});

test("proxy.ts source wires the fail-closed 503 branch (regression guard)", () => {
  const src = readFileSync(
    fileURLToPath(new URL("../../../../proxy.ts", import.meta.url)),
    "utf8",
  );
  // The deployed-without-token branch must return a 503 refusal, not NextResponse.next().
  assert.match(src, /isDeployed\(\)/, "proxy must branch on the deployed env signal");
  assert.match(src, /status:\s*503/, "proxy must refuse with a 503 in the fail-closed branch");
  assert.match(src, /process\.env\.ENVIRONMENT/, "proxy must read the ENVIRONMENT mirror var");
});
