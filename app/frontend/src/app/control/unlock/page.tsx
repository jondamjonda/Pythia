"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

// The control-room lock screen (rendered chrome-free). Shown only when CONTROL_PANEL_TOKEN is configured;
// posts the token to the same-origin auth route, which sets the httpOnly cookie server-side.
// useSearchParams() must sit under a Suspense boundary (Next 16), so the form is split out.
export default function Unlock() {
  return (
    <Suspense fallback={<div className="min-h-screen" />}>
      <UnlockForm />
    </Suspense>
  );
}

function UnlockForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") ?? "/control";
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/control/api/auth", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!r.ok) {
        setError("Incorrect token.");
        setBusy(false);
        return;
      }
      router.replace(next);
    } catch {
      setError("Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border p-6"
        style={{ background: "var(--ctl-panel)", borderColor: "var(--ctl-border)" }}
      >
        <h1 className="text-base font-semibold">Control room</h1>
        <p className="mt-1 text-[12px]" style={{ color: "var(--ctl-muted)" }}>
          Enter the operator token to continue.
        </p>
        <input
          type="password"
          autoFocus
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="control panel token"
          className="tnum mt-4 w-full rounded-md border px-3 py-2 text-[13px] outline-none"
          style={{ background: "#0c0c0f", borderColor: "var(--ctl-border)", color: "var(--ctl-text)" }}
        />
        {error && (
          <p className="mt-2 text-[12px]" style={{ color: "var(--ctl-critical)" }}>
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="mt-4 w-full rounded-md px-3 py-2 text-[13px] font-medium disabled:opacity-50"
          style={{ background: "#27272a", color: "var(--ctl-text)" }}
        >
          {busy ? "unlocking…" : "Unlock"}
        </button>
      </form>
    </div>
  );
}
