"use client";
// UnderstandingPanel (T7) — the collapsed "Jak jsme otázce porozuměli" panel that
// surfaces HOW the engine understood the layman question, rendered ONLY when the
// answer payload carries a T7 `intake` block (server flag ON + engine intake_v2).
// When the block is absent (today, or the flag is OFF) the caller (AnswerView)
// renders EXACTLY today's essay view — this component adds behaviour, never removes.
//
// WHAT IT SHOWS (all from the whitelisted `intake` block — never fabricated):
//   - the register `sub_questions` the engine decomposed the query into;
//   - the extracted `facts`, each badged by its `source` (det | llm | profile);
//   - the `conflicts` the intake flagged (inert key/value pairs);
//   - a `sufficiency` verdict;
//   - a subtle "co jsme nevzali v potaz" list from `dropped[]` (each with its DRAFT
//     machine reason) — honest about what the verifier did NOT take into account.
//
// PLUS the CONSENTED profile write-back: the asker's own whitelisted facts
// (`profile_suggestions`) offered with a one-click "uložit do profilu" affordance.
// The save is NEVER automatic — it POSTs to the existing profiles PATCH endpoint
// ONLY on explicit click, and ONLY when a `profileId` is in scope. With no profileId
// the affordance is DISABLED with an honest "spojte s profilem" note (deferred).
//
// The PURE logic + copy live in the sibling `understanding.ts` (unit-testable under
// the bare Node test runner); this file is the render layer only.
//
// HARD INVARIANTS (mirror AnswerView / PracticalPlan):
//  - XSS / OUTPUT-SAFETY: every server/model string (sub-questions, fact values,
//    conflict text, reasons) is rendered as React text children only → HTML-escaped,
//    inert. No dangerouslySetInnerHTML, no HTML/markdown parsing.
//  - UNKNOWN-SHAPE SAFE: a missing/empty field renders nothing; a block with nothing
//    renderable makes the panel render nothing (the essay view is byte-identical).
//  - DRAFT HONESTY (G14): the dropped reasons are the server's DRAFT copy; the only
//    fixed UI chrome is the panel/section headings + the source/draft badges.
import { useState } from "react";

import {
  UNDERSTANDING_COPY as COPY,
  buildPatch,
  conflictText,
  fieldLabel,
  hasContent,
  sourceLabel,
} from "@/components/answer/understanding";
import { api } from "@/lib/api";
import type {
  AnswerResponse,
  IntakeConflict,
  IntakeDropped,
  IntakeFact,
  ProfileSuggestion,
} from "@/lib/api";

function SourceBadge({ source }: { source?: string | null }) {
  const label = sourceLabel(source);
  if (!label) return null;
  return (
    <span className="ml-2 inline-flex items-center rounded-full border border-slate-300 bg-slate-50 px-2 py-0.5 align-middle text-[11px] font-medium text-slate-600">
      {label}
    </span>
  );
}

function SubQuestions({ items }: { items: string[] }) {
  const clean = items.filter((q) => typeof q === "string" && q.trim());
  if (clean.length === 0) return null;
  return (
    <section className="mt-4">
      <h3 className="text-[13px] font-semibold uppercase tracking-wide text-slate-500">
        {COPY.subQuestions}
      </h3>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-[14px] leading-relaxed text-slate-700">
        {clean.map((q, i) => (
          <li key={i}>{q.trim()}</li>
        ))}
      </ul>
    </section>
  );
}

function Facts({ facts }: { facts: IntakeFact[] }) {
  const clean = facts.filter((f) => f && f.field && (f.value ?? "").toString().trim());
  if (clean.length === 0) return null;
  return (
    <section className="mt-4">
      <h3 className="text-[13px] font-semibold uppercase tracking-wide text-slate-500">
        {COPY.facts}
      </h3>
      <dl className="mt-2 space-y-1.5">
        {clean.map((f, i) => (
          <div key={i} className="flex flex-wrap items-baseline gap-x-2 text-[14px]">
            <dt className="font-medium text-slate-900">{fieldLabel(f.field)}</dt>
            <dd className="text-slate-700">{String(f.value)}</dd>
            <SourceBadge source={f.source} />
          </div>
        ))}
      </dl>
    </section>
  );
}

function Conflicts({ conflicts }: { conflicts: IntakeConflict[] }) {
  const lines = conflicts
    .filter((c) => c && typeof c === "object")
    .map((c) => conflictText(c))
    .filter((t) => t);
  if (lines.length === 0) return null;
  return (
    <section className="mt-4">
      <h3 className="text-[13px] font-semibold uppercase tracking-wide text-amber-700">
        {COPY.conflicts}
      </h3>
      <ul className="mt-2 space-y-1 text-[14px] leading-relaxed text-amber-800">
        {lines.map((t, i) => (
          <li key={i} className="whitespace-pre-wrap">
            {t}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Sufficiency({ value }: { value?: string | null }) {
  const v = (value ?? "").trim();
  if (!v) return null;
  return (
    <p className="mt-4 text-[13px] text-slate-500">
      <span className="font-medium text-slate-600">{COPY.sufficiency}:</span> {v}
    </p>
  );
}

function Dropped({ dropped }: { dropped: IntakeDropped[] }) {
  const clean = dropped.filter((d) => d && d.field);
  if (clean.length === 0) return null;
  return (
    <section className="mt-5 border-t border-slate-100 pt-3">
      <h3 className="text-[12px] font-semibold uppercase tracking-wide text-slate-400">
        {COPY.dropped}
      </h3>
      <p className="mt-1 text-[12px] text-slate-400">{COPY.droppedHint}</p>
      <ul className="mt-2 space-y-1 text-[13px] leading-relaxed text-slate-500">
        {clean.map((d, i) => (
          <li key={i}>
            <span className="font-medium text-slate-600">{fieldLabel(d.field)}</span>
            {d.reason && d.reason.trim() ? (
              <span className="text-slate-400"> — {d.reason.trim()}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

// The consented write-back affordance. Functional ONLY when a `profileId` is in scope
// (the answer surface may not have one — then the button is disabled with an honest
// note). NEVER auto-saves: the PATCH fires only on click.
function ProfileWriteBack({
  suggestions,
  profileId,
}: {
  suggestions: ProfileSuggestion[];
  profileId?: string | null;
}) {
  const [state, setState] = useState<"idle" | "saving" | "done" | "error">("idle");
  const [errText, setErrText] = useState<string | null>(null);

  const clean = suggestions.filter((s) => s && s.field && (s.value ?? "").toString().trim());
  if (clean.length === 0) return null;

  const canSave = Boolean(profileId) && state !== "saving" && state !== "done";

  async function onSave() {
    if (!profileId) return;
    const { body, count } = buildPatch(clean);
    if (count === 0) return;
    setState("saving");
    setErrText(null);
    try {
      await api.patchProfile(profileId, body);
      setState("done");
    } catch (e) {
      setState("error");
      setErrText(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="mt-5 rounded-md border border-slate-200 bg-slate-50 px-3 py-3">
      <h3 className="text-[13px] font-semibold text-slate-700">{COPY.saveTitle}</h3>
      <p className="mt-1 text-[12px] text-slate-500">{COPY.saveHint}</p>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {clean.map((s, i) => (
          <li
            key={i}
            className="inline-flex items-center rounded-full border border-slate-300 bg-white px-2 py-0.5 text-[12px] text-slate-700"
          >
            <span className="font-medium">{fieldLabel(s.field)}</span>
            <span className="ml-1 text-slate-500">{String(s.value)}</span>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={onSave}
          disabled={!canSave}
          className="inline-flex items-center rounded-md border border-slate-300 bg-white px-3 py-1.5 text-[13px] font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {state === "saving"
            ? COPY.saveBtnSaving
            : state === "done"
              ? COPY.saveBtnDone
              : COPY.saveBtn}
        </button>
        {!profileId && (
          <span className="text-[12px] text-slate-400">{COPY.saveDisabledNote}</span>
        )}
        {state === "error" && errText && (
          <span className="text-[12px] text-red-600">{errText}</span>
        )}
      </div>
    </section>
  );
}

// The public renderer used by AnswerView. Returns null when there is no intake block
// (or nothing renderable) → the caller renders today's essay view byte-identically.
export function UnderstandingPanel({
  answer,
  profileId,
}: {
  answer: AnswerResponse;
  profileId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const block = answer.intake;
  if (!hasContent(block)) return null;

  const suggestions = (answer.profile_suggestions ?? []).filter(Boolean);

  return (
    <section className="mt-8 rounded-lg border border-slate-200" aria-label={COPY.panelTitle}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left"
      >
        <span className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-700">{COPY.panelTitle}</span>
          <span className="inline-flex items-center rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800">
            {COPY.draftBadge}
          </span>
        </span>
        <span className="text-slate-400" aria-hidden="true">
          {open ? "−" : "+"}
        </span>
      </button>
      {open && (
        <div className="border-t border-slate-100 px-4 pb-4 pt-2">
          <p className="text-[12px] text-slate-400">{COPY.panelHint}</p>
          <SubQuestions items={block?.sub_questions ?? []} />
          <Facts facts={block?.facts ?? []} />
          <Conflicts conflicts={block?.conflicts ?? []} />
          <Sufficiency value={block?.sufficiency} />
          <Dropped dropped={block?.dropped ?? []} />
          {suggestions.length > 0 && (
            <ProfileWriteBack suggestions={suggestions} profileId={profileId} />
          )}
        </div>
      )}
    </section>
  );
}
