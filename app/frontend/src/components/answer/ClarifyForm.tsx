"use client";

// ClarifyForm (Track A / VL-A / E12) — the FE mirror of the STAGE-1 clarify frame the
// backend ships (src/regsuper/api/answer.py, render_mode="clarify"). A clarify is NOT a
// refusal and NOT a non-answer (refused=false): the server needs ≤3 decision-relevant
// facts before it can answer accurately. This renders the honest information-not-advice
// `disclaimer` + the ≤3 `questions` as a slot-collection FORM, then RE-POSTS the SAME
// query with the collected answers as `slots` (MAX 1 round server-side) and hands the
// SECOND answer back to the caller to render IN PLACE. Never a dead-end.
//
// The wire `ClarifyQuestion` carries only { slot, text } (no options). For the slots we
// KNOW the closed value set (the boolean gates + a few enumerated slots — a minimal mirror
// of the backend SLOT_ENUMS), we render a <select> so the user cannot submit an out-of-enum
// value the server would reject; every other slot (e.g. free-text region) gets a text input.
import { useState } from "react";
import { api, type AnswerResponse, type ClarifyQuestion } from "@/lib/api";

// A MINIMAL mirror of the backend SLOT_ENUMS (regsuper.analysis.clarify). Only the closed
// value sets — a slot absent here is free-text (rendered as a text input). Kept small +
// explicit; the server re-validates the resubmitted `slots` against the authoritative enums,
// so an out-of-date mirror can only under-constrain the UI, never send an invalid answer the
// server would silently accept. Labels are DRAFT CZ display copy for the options.
const YES_NO: { value: string; label: string }[] = [
  { value: "yes", label: "Ano" },
  { value: "no", label: "Ne" },
];
const SLOT_OPTIONS: Record<string, { value: string; label: string }[]> = {
  legal_form: [
    { value: "sro", label: "s.r.o." },
    { value: "as", label: "a.s." },
    { value: "osvc", label: "OSVČ" },
    { value: "ks", label: "k.s." },
    { value: "vos", label: "v.o.s." },
    { value: "se", label: "SE" },
    { value: "druzstvo", label: "družstvo" },
    { value: "other", label: "jiná" },
  ],
  headcount_band: [
    { value: "1", label: "1" },
    { value: "2-9", label: "2–9" },
    { value: "10-49", label: "10–49" },
    { value: "50-249", label: "50–249" },
    { value: "250+", label: "250+" },
  ],
  is_employer: YES_NO,
  processes_personal_data: YES_NO,
  cross_border: YES_NO,
  handles_client_money: YES_NO,
  has_real_estate: YES_NO,
  // region is free-text (thousands of municipalities) — deliberately absent → text input.
};

export function ClarifyForm({
  answer,
  query,
  asOf,
  projectId,
  onAnswer,
}: {
  answer: AnswerResponse;
  // The ORIGINAL query is re-POSTed verbatim on the follow-up (single-shot invariant —
  // history is never concatenated; only `slots` is added).
  query: string;
  asOf?: string | null;
  projectId?: string | null;
  // The caller swaps the displayed answer to the SECOND (sound) response, rendered in place.
  onAnswer: (next: AnswerResponse) => void;
}) {
  const questions: ClarifyQuestion[] = (answer.questions ?? []).filter(
    (q): q is ClarifyQuestion => Boolean(q && q.slot),
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function setValue(slot: string, v: string) {
    setValues((prev) => ({ ...prev, [slot]: v }));
  }

  // The collected, non-empty answers keyed by slot — exactly the follow-up `slots` map.
  function collectSlots(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const q of questions) {
      const v = (values[q.slot] ?? "").trim();
      if (v) out[q.slot] = v;
    }
    return out;
  }

  const slots = collectSlots();
  // Require an answer to every asked question before resubmit (the server treats a still-
  // missing REQUIRED slot as an assumption to hedge over; we prefer a complete follow-up).
  const allAnswered = questions.length > 0 && questions.every((q) => Boolean(slots[q.slot]));

  async function submit() {
    if (!allAnswered || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      // Re-POST the SAME query with the collected answers as `slots` (turn 2, MAX 1 round).
      const next = await api.answer(query, {
        ...(asOf ? { as_of: asOf } : {}),
        ...(projectId ? { project_id: projectId } : {}),
        slots,
      });
      onAnswer(next);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section
      className="mt-6 rounded-xl border border-sky-200 bg-sky-50 p-5"
      aria-label="Upřesnění dotazu"
    >
      <h2 className="text-base font-semibold text-slate-900">
        {answer.title?.trim() || "Potřebujeme upřesnit několik údajů"}
      </h2>

      {/* The honest information-not-advice framing the server supplies (escaped text). */}
      {answer.body?.trim() && (
        <p className="mt-2 text-sm text-slate-700 whitespace-pre-wrap">{answer.body}</p>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="mt-4 space-y-4"
      >
        {questions.map((q) => {
          const options = SLOT_OPTIONS[q.slot];
          const inputId = `clarify-${q.slot}`;
          return (
            <div key={q.slot} className="flex flex-col gap-1.5">
              <label htmlFor={inputId} className="text-sm font-medium text-slate-800">
                {q.text}
              </label>
              {options ? (
                <select
                  id={inputId}
                  name={q.slot}
                  value={values[q.slot] ?? ""}
                  onChange={(e) => setValue(q.slot, e.target.value)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-[15px] text-slate-900 outline-none focus:border-slate-400"
                >
                  <option value="">— vyberte —</option>
                  {options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={inputId}
                  name={q.slot}
                  type="text"
                  value={values[q.slot] ?? ""}
                  onChange={(e) => setValue(q.slot, e.target.value)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-[15px] text-slate-900 placeholder:text-slate-400 outline-none focus:border-slate-400"
                  placeholder="Vaše odpověď"
                />
              )}
            </div>
          );
        })}

        {error && (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!allAnswered || submitting}
            className="rounded-lg bg-slate-900 px-5 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {submitting ? "Odesílám…" : "Odpovědět a pokračovat →"}
          </button>
        </div>
      </form>

      {/* The constant information-not-advice disclaimer the clarify frame always carries. */}
      {answer.disclaimer?.trim() && (
        <p className="mt-4 border-t border-sky-200/70 pt-3 text-xs leading-relaxed text-slate-500">
          {answer.disclaimer}
        </p>
      )}
    </section>
  );
}
