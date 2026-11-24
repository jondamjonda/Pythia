"use client";

// Answer surface (M8) — the first real consumer "ask a legal question" screen.
// A query box (+ optional as_of point-in-time date) → POST /api/answer → the
// six-render_mode AnswerView.
//
// DEFAULT-OFF / BYTE-IDENTICAL: this page only exists behind the FE env flag
// NEXT_PUBLIC_ANSWER_VIEW_ENABLED. When the flag is off the route renders an inert
// placeholder (and the nav never links here — see layout/ControlChrome). The REAL
// gate is the server flag `answer_endpoint_enabled`: when off the API returns 404,
// which we surface as the honest off-state, never as a fake answer.
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, type AnswerResponse } from "@/lib/api";
import { AnswerView } from "@/components/answer/AnswerView";

const ANSWER_VIEW_ENABLED =
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_ANSWER_VIEW_ENABLED === "true";

function isNotFound(message: string): boolean {
  // The server gate (answer_endpoint_enabled=False) returns 404; api.ts throws
  // an Error whose message embeds the status. Treat that as the off-state.
  return / → 404:/.test(message) || /\b404\b/.test(message);
}

function OffState() {
  return (
    <main className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
        Odpovědi zatím nejsou k dispozici
      </h1>
      <p className="mt-2 text-slate-500">
        Tato funkce je momentálně vypnutá.
      </p>
    </main>
  );
}

// useSearchParams() must sit under a Suspense boundary (Next 16), so the page body
// (which reads the optional `?profileId=` / `?project_id=` link param) is split out.
export default function AnswerPage() {
  return (
    <Suspense fallback={<main className="mx-auto max-w-3xl px-4 py-12" />}>
      <AnswerPageBody />
    </Suspense>
  );
}

function AnswerPageBody() {
  // The optional profile the ask is answered FOR: threads `project_id` onto the answer
  // request (profile-context personalization) and `profileId` into <AnswerView> so the
  // T7 write-back SUGGESTIONS have a profile to save into. Accepts either key. Absent →
  // an anonymous ask exactly as before (byte-identical).
  const params = useSearchParams();
  const profileId = params.get("profileId") ?? params.get("project_id") ?? null;

  const [query, setQuery] = useState("");
  const [asOf, setAsOf] = useState("");
  const [loading, setLoading] = useState(false);
  const [res, setRes] = useState<AnswerResponse | null>(null);
  // The EXACT query that produced `res` — the clarify follow-up re-POSTs THIS verbatim
  // (not the live textarea, which the user may have since edited). asOf is captured too.
  const [askedQuery, setAskedQuery] = useState("");
  const [askedAsOf, setAskedAsOf] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [serverOff, setServerOff] = useState(false);

  // FE flag off → inert placeholder; nav never links here, existing pages untouched.
  if (!ANSWER_VIEW_ENABLED) return <OffState />;

  async function ask(q: string) {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setRes(null);
    setAskedQuery(q);
    setAskedAsOf(asOf);
    try {
      // Send `project_id` when a profile is linked so the answer is personalized to it
      // and the write-back suggestions come alive; omitted otherwise (anonymous ask).
      const answer = await api.answer(q, {
        ...(asOf ? { as_of: asOf } : {}),
        ...(profileId ? { project_id: profileId } : {}),
      });
      setRes(answer);
    } catch (err) {
      const msg = (err as Error).message;
      if (isNotFound(msg)) {
        // Server gate off → honest off-state, not an error.
        setServerOff(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }

  // The server endpoint is gated off → render the same honest off-state.
  if (serverOff) return <OffState />;

  return (
    <main className="mx-auto max-w-3xl px-4 py-12">
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
          Zeptejte se na právní otázku
        </h1>
        <p className="mt-1 text-slate-500">
          Položte konkrétní otázku k českým a EU předpisům. Odpověď je rozdělena do
          sekcí a každé tvrzení je doloženo odkazem na konkrétní ustanovení.
        </p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          ask(query);
        }}
        className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
      >
        <textarea
          className="h-28 w-full resize-none rounded-lg border border-slate-200 bg-white px-3 py-2 text-[15px] text-slate-900 placeholder:text-slate-400 outline-none focus:border-slate-400"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Např. Jaké povinnosti má depozitář fondu při nabízení AIF retailovým investorům v ČR?"
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-[13px] text-slate-500">
            Ke dni
            <input
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              className="rounded-md border border-slate-200 px-2 py-1 text-[13px] text-slate-700 outline-none focus:border-slate-400"
            />
          </label>
          <button
            type="submit"
            disabled={loading || !query.trim()}
            className="ml-auto rounded-lg bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {loading ? "Hledám…" : "Získat odpověď →"}
          </button>
        </div>
      </form>

      {error && (
        <div className="mt-6 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      {res && (
        <AnswerView
          answer={res}
          profileId={profileId}
          query={askedQuery}
          asOf={askedAsOf || null}
          onAnswer={setRes}
        />
      )}
    </main>
  );
}
