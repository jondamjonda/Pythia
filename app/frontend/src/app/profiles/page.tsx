"use client";

// Profiles list (LEDGER-M9) — the SME's saved business profiles. The consumer
// entry to the digest flow: describe your business once, then open its digest.
//
// DEFAULT-OFF / BYTE-IDENTICAL: this page only exists behind the FE env flag
// NEXT_PUBLIC_DIGEST_UI_ENABLED. Off → an inert placeholder, and the nav never
// links here (see page.tsx footer). The REAL gate is the server flags
// `profiles_enabled` / `digest_enabled`: when off the APIs 404, surfaced here as
// the honest off-state — never an error toast.
//
// NOTE: the backend exposes create/get/analyze per profile but NO "list all"
// endpoint. So the list is sourced from the profiles this browser created
// (remembered in localStorage) + an "open by id" box — never a fabricated list.
import Link from "next/link";
import { useEffect, useState } from "react";

const DIGEST_UI_ENABLED =
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "true";

// Shared with profiles/new — the locally-remembered profiles (id + name).
export const RECENT_PROFILES_KEY = "polarius.recentProfiles";
export interface RecentProfile {
  id: string;
  name: string;
}

function readRecent(): RecentProfile[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_PROFILES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (x): x is RecentProfile =>
        !!x && typeof x === "object" && typeof (x as RecentProfile).id === "string",
    );
  } catch {
    return [];
  }
}

function OffState() {
  return (
    <main className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
        Přehled pro firmy zatím není k dispozici
      </h1>
      <p className="mt-2 text-slate-500">Tato funkce je momentálně vypnutá.</p>
    </main>
  );
}

export default function ProfilesPage() {
  const [recent, setRecent] = useState<RecentProfile[]>([]);
  const [openId, setOpenId] = useState("");

  useEffect(() => {
    // Yield once so the setState is not synchronous in the effect body
    // (react-hooks/set-state-in-effect). Reading localStorage is client-only.
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled) setRecent(readRecent());
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!DIGEST_UI_ENABLED) return <OffState />;

  return (
    <main className="mx-auto max-w-3xl px-4 py-12">
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
          Přehled povinností pro vaši firmu
        </h1>
        <p className="mt-1 text-slate-500">
          Popište svou firmu jednou a získejte srozumitelný přehled povinností,
          mezer a doporučených kroků — vše s odkazem na konkrétní předpis.
        </p>
      </header>

      <Link
        href="/profiles/new"
        className="inline-flex rounded-lg bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-700"
      >
        Popsat firmu →
      </Link>

      {recent.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Naposledy vytvořené profily
          </h2>
          <ul className="mt-3 space-y-2">
            {recent.map((p) => (
              <li key={p.id}>
                <Link
                  href={`/digest/${p.id}`}
                  className="block rounded-lg border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300"
                >
                  <span className="font-medium text-slate-900">{p.name}</span>
                  <span className="ml-2 font-mono text-[11px] text-slate-400">
                    {p.id.slice(0, 8)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Otevřít přehled podle ID profilu
        </h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const id = openId.trim();
            if (id) window.location.assign(`/digest/${encodeURIComponent(id)}`);
          }}
          className="mt-3 flex gap-2"
        >
          <input
            value={openId}
            onChange={(e) => setOpenId(e.target.value)}
            placeholder="ID profilu (UUID)"
            className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 font-mono text-[13px] text-slate-900 placeholder:text-slate-400 outline-none focus:border-slate-400"
          />
          <button
            type="submit"
            disabled={!openId.trim()}
            className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            Otevřít →
          </button>
        </form>
      </section>
    </main>
  );
}
