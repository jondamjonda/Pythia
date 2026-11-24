// AnswerSection (M8) — renders ONE answer section: heading + the LLM-drafted
// prose + the section's citation chips.
//
// XSS / OUTPUT-SAFETY (cardinal invariant): the prose is model output. It is
// rendered as React text children only — React HTML-escapes text nodes, so a
// crafted draft containing `<script>…</script>` or any markup renders as VISIBLE
// TEXT and never executes. We deliberately do NOT use dangerouslySetInnerHTML and
// do NOT parse the draft as HTML/markdown here (no sanitizer dependency exists in
// this app, and the safe default is inert text). Paragraph breaks are the only
// formatting: we split on blank lines and emit <p> elements whose children are
// plain (escaped) strings.
import { ChipRow } from "@/components/answer/ChipResolver";
import type { AnswerSection as Section } from "@/lib/api";

function Prose({ text }: { text: string }) {
  // Split into paragraphs on blank lines; each paragraph is a plain string child
  // (escaped by React). No HTML, no markdown parsing → inert by construction.
  const paras = text.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  if (paras.length === 0) return null;
  return (
    <div className="space-y-2 text-[15px] leading-relaxed text-slate-800">
      {paras.map((p, i) => (
        <p key={i} className="whitespace-pre-wrap">{p}</p>
      ))}
    </div>
  );
}

export function AnswerSection({ section }: { section: Section }) {
  const text = section.text ?? "";
  const hasProse = text.trim().length > 0;
  const chips = section.chips ?? [];
  // Render nothing for a fully empty section (e.g. an absent Judikatura section
  // arrives as no section at all upstream; this is a defensive belt).
  if (!hasProse && chips.length === 0) return null;
  return (
    <section className="mt-6">
      {section.heading && (
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          {section.heading}
        </h2>
      )}
      {hasProse && (
        <div className="mt-2">
          <Prose text={text} />
        </div>
      )}
      <ChipRow chips={chips} />
    </section>
  );
}
