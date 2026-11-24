// PracticalPlan (B1c-fe) — the practical FACTS sidecar the answer surface renders
// UNDER the essay sections when the payload carries a structured `practical` block.
//
// The seven-slot PROSE (situation → applies → obligations → steps → where → risks
// → gaps) is NOT rendered here — it rides `answer.sections[]` and is rendered by
// AnswerView/AnswerSection above. This component renders the STRUCTURED sidecar the
// engine attaches to that prose:
//   - `items`  → grounded FACTS grouped by slot, each with its verbatim excerpt and
//                its provision citation chip (the same ChipResolver the sections use);
//   - `gaps`   → honest DRAFT non-claims, each rendered with the visible
//                "orientační — ověřte" badge (advocate-review-pending, G14);
//   - `demoted_slots` → a caution noting which slots the server demoted (an item was
//                dropped as un-grounded, or a slot failed slot-verify) — surfaced, not hidden.
//
// HARD INVARIANTS (mirror AnswerView / AnswerSection):
//  - UNKNOWN-SLOT FALLBACK: an item whose `slot` is not one of the seven known keys
//    still renders under a group labelled with the raw slot key. We NEVER crash on
//    and NEVER hide an unrecognised slot.
//  - XSS / OUTPUT-SAFETY: every server/model string (headings, value_raw, gap text,
//    slot keys) is rendered as React text children only → HTML-escaped, inert. No
//    dangerouslySetInnerHTML, no HTML/markdown parsing.
//  - DRAFT HONESTY (G14): gap lines are always badged; we invent no legal copy — all
//    Czech strings come from the payload, with only known-slot fallback headings +
//    the draft badge + the demotion caution as fixed UI chrome.
//  - CITATION GROUNDING: a grounded item renders its provision chip via the same
//    ChipResolver used by the essay sections (deep-links into the source viewer).
//  - NULL-WHEN-NULL: a missing item/gap/heading renders nothing, never a stub. A
//    block with nothing renderable makes getPracticalBlock return null → the caller
//    renders today's essay view byte-identically.
import { ChipResolver } from "@/components/answer/ChipResolver";
import {
  gapText,
  getPracticalBlock,
  groupItemsBySlot,
  itemChip,
  slotHeading,
  PRACTICAL_DRAFT_BADGE,
  type PracticalBlock,
  type PracticalGap,
  type PracticalItem,
  type PracticalSlotGroup,
  type PracticalSlotKind,
} from "@/components/answer/practical";
import type { AnswerResponse } from "@/lib/api";

// A small amber DRAFT / advocate-review-pending pill. Fixed UI chrome (not legal
// substance): safe to author here under the G14 posture.
function DraftBadge({ text }: { text?: string | null }) {
  return (
    <span
      className="ml-2 inline-flex items-center rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 align-middle text-[11px] font-medium text-amber-800"
      title="Návrh — před použitím ověřte u advokáta."
    >
      {text && text.trim() ? text : PRACTICAL_DRAFT_BADGE}
    </span>
  );
}

// One grounded fact line: the verbatim excerpt (+ optional fact-type label) and its
// provision citation chip. A line with nothing to show renders nothing.
function FactLine({ item }: { item: PracticalItem }) {
  const value = (item.value_raw ?? "").trim();
  const factType = (item.fact_type ?? "").trim();
  const chip = itemChip(item);
  if (!value && !factType && !chip) return null;
  return (
    <li className="text-[15px] leading-relaxed text-slate-800">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {factType && <span className="font-medium text-slate-900">{factType}</span>}
        {value && <span className="whitespace-pre-wrap">{value}</span>}
      </div>
      {chip && (
        <div className="mt-1 flex flex-wrap gap-1.5">
          <ChipResolver chip={chip} />
        </div>
      )}
    </li>
  );
}

// One slot group: its heading (known-slot fallback, else the raw slot key for an
// unknown slot, else a neutral label for a slot-less bucket) and its fact lines.
function SlotGroup({ group }: { group: PracticalSlotGroup }) {
  // A group with no renderable lines renders nothing.
  const lines = group.items.filter(Boolean);
  if (lines.length === 0) return null;
  // A slot-less bucket ("" key) gets a neutral heading rather than an empty string.
  const heading = group.heading.trim() ? group.heading : "Doložené údaje";
  return (
    <section className="mt-5">
      <h3 className="text-[13px] font-semibold uppercase tracking-wide text-slate-500">
        {heading}
      </h3>
      <ul className="mt-2 space-y-2">
        {lines.map((it, i) => (
          <FactLine key={i} item={it} />
        ))}
      </ul>
    </section>
  );
}

// The honest DRAFT gap lines. Always badged. A gap with no text at all is skipped
// (never a blank badged stub).
function GapLines({ gaps }: { gaps: PracticalGap[] }) {
  const lines = gaps
    .filter(Boolean)
    .map((g) => ({ gap: g, text: gapText(g) }))
    .filter((x) => x.text);
  if (lines.length === 0) return null;
  return (
    <section className="mt-5">
      <h3 className="flex flex-wrap items-center text-[13px] font-semibold uppercase tracking-wide text-slate-500">
        <span>{slotHeading("gaps")}</span>
        <DraftBadge />
      </h3>
      <ul className="mt-2 space-y-2">
        {lines.map((x, i) => (
          <li key={i} className="text-[15px] leading-relaxed text-slate-800">
            <span className="whitespace-pre-wrap">{x.text}</span>
            <DraftBadge />
          </li>
        ))}
      </ul>
    </section>
  );
}

// The demotion caution: which slots the server demoted (an un-grounded item dropped,
// or a slot-verify failure). Machine slot keys mapped to their CZ headings so the
// caution reads plainly; renders nothing when there are no demotions.
function DemotionCaution({ slots }: { slots: PracticalSlotKind[] }) {
  const clean = Array.from(
    new Set(slots.filter((s) => typeof s === "string" && s.trim()).map((s) => s.trim())),
  );
  if (clean.length === 0) return null;
  const labels = clean.map((s) => slotHeading(s)).join(", ");
  return (
    <p className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] leading-relaxed text-amber-800">
      Některé části se nepodařilo spolehlivě doložit a byly potlačeny: {labels}.
    </p>
  );
}

// The public renderer used by AnswerView. Returns null when there is no practical
// block → the caller renders today's essay view byte-identically.
export function PracticalPlan({ answer }: { answer: AnswerResponse }) {
  const block = getPracticalBlock(answer);
  if (!block) return null;
  return <PracticalPlanBlock block={block} />;
}

function PracticalPlanBlock({ block }: { block: PracticalBlock }) {
  const items = (block.items ?? []).filter(Boolean) as PracticalItem[];
  const gaps = (block.gaps ?? []).filter(Boolean) as PracticalGap[];
  const demoted = (block.demoted_slots ?? []).filter(Boolean) as PracticalSlotKind[];
  const groups = groupItemsBySlot(items);

  return (
    <section className="mt-8 border-t border-slate-200 pt-5" aria-label="Praktické údaje">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600">
        Praktické údaje
      </h2>
      <div className="mt-1">
        {groups.map((g) => (
          <SlotGroup key={g.slot || "__unslotted__"} group={g} />
        ))}
        {gaps.length > 0 && <GapLines gaps={gaps} />}
        {demoted.length > 0 && <DemotionCaution slots={demoted} />}
      </div>
    </section>
  );
}
