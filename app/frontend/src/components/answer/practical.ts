// B1c (frontend) — the LOCAL, defensive type surface for the optional structured
// "practical" action-plan sidecar the answer payload MAY now carry alongside the
// essay `sections`.
//
// THE REAL CONTRACT (matches backend PracticalBlockOut in
// src/regsuper/api/answer.py). The block is FLAT — it is NOT a `slots[]` tree:
//
//   practical: {
//     items:         PracticalItem[]   // grounded facts, each keyed to a slot
//     gaps:          PracticalGap[]    // honest DRAFT non-claims (freeform dicts)
//     demoted_slots: string[]          // slot keys the server demoted (caution)
//     profile_used:  boolean           // an A2b business profile seeded `situation`
//   }
//
// Each item = { slot, fact_type, value_raw?, provision_id?, grounded }. The SEVEN-
// SLOT PROSE (situation/applies/obligations/steps/where/risks/gaps) does NOT live
// here — it rides `answer.sections[]` as ordinary sections and is rendered by
// AnswerView/AnswerSection (with the same unknown-kind fallback). This sidecar
// only carries the grounded FACTS to hang under those slots, the honest gaps, and
// the demotion caution.
//
// WHY LOCAL (not in src/lib/api.ts): the wire shape of `practical` is owned by the
// sibling backend unit (B1c-be). Rather than widen the shared api.ts contract from
// the frontend unit (collision risk + we do not own that file), we read the block
// off the existing AnswerResponse via a defensive accessor and model it here. Every
// field is optional / null-tolerant: a missing block, a missing slot, or an
// unrecognised slot key must render nothing extra / fall back to a plain grouped
// list — never crash, never hide content, never fabricate copy.
//
// When the payload carries NO `practical` block (today), the AnswerView renders
// EXACTLY today's essay view (byte-identical). This module only adds behaviour.
import type { AnswerChip, AnswerResponse } from "@/lib/api";

// The seven action-plan slot keys the engine routes facts into. `string` keeps the
// union open so an unknown slot from a newer server still type-checks and hits the
// "unknown-slot" grouped fallback.
export type PracticalSlotKind =
  | "situation" // Vaše situace
  | "applies" // Co se na vás vztahuje
  | "obligations" // Vaše povinnosti a lhůty
  | "steps" // Jak postupovat
  | "where" // Kam se obrátit
  | "risks" // Rizika a sankce
  | "gaps" // Co se nepodařilo doložit (DRAFT)
  | string;

// One grounded practical FACT routed to a slot — mirrors the backend
// PracticalItemOut exactly (slot / fact_type / value_raw / provision_id / grounded).
// `value_raw` is the VERBATIM provision excerpt (rendered as inert, escaped text).
export interface PracticalItem {
  slot?: PracticalSlotKind | null;
  fact_type?: string | null;
  value_raw?: string | null;
  provision_id?: string | null;
  grounded?: boolean | null;
}

// One honest DRAFT gap. The backend passes `gaps` through as freeform dicts (it
// only reads `gap_class` / `status` for telemetry), so we model it permissively:
// any of a few likely text keys may carry a display line, and a class/status may
// label it. We NEVER invent legal copy — if no text key is present the gap renders
// only its class/status label so nothing is silently dropped.
export interface PracticalGap {
  gap_class?: string | null;
  status?: string | null;
  slot?: PracticalSlotKind | null;
  // The human-readable gap line, if the server supplied one (tolerant of the few
  // keys the engine might use — first non-empty wins at render time).
  text?: string | null;
  label?: string | null;
  note?: string | null;
  message?: string | null;
  [key: string]: unknown;
}

export interface PracticalBlock {
  items?: PracticalItem[] | null;
  gaps?: PracticalGap[] | null;
  demoted_slots?: PracticalSlotKind[] | null;
  profile_used?: boolean | null;
}

// The optional `practical` sidecar rides the AnswerResponse but is NOT declared on the
// shared api.ts contract (owned by the sibling backend unit — see the module header).
// Rather than reach in via an untyped `as unknown as { practical?: unknown }` double
// cast (which erases the AnswerResponse type and models the field as bare `unknown`),
// we model the exact wire augmentation HERE and read the block through it. This keeps
// the accessor typed (tsc checks the field against `PracticalBlock`) without widening
// api.ts. Runtime behaviour is unchanged: the accessor still validates the shape at
// runtime and returns null on anything non-renderable.
export type AnswerWithPractical = AnswerResponse & {
  practical?: PracticalBlock | null;
};

// The canonical render order for the seven slots. Items are grouped and shown in
// this order; unknown slot keys sink to the end (never dropped).
export const PRACTICAL_SLOT_ORDER: PracticalSlotKind[] = [
  "situation",
  "applies",
  "obligations",
  "steps",
  "where",
  "risks",
  "gaps",
];

// Sensible CZ fallback headings — used to label a slot group. These are DRAFT /
// advocate-review-pending copy under the G14 posture. For an UNKNOWN slot key we do
// NOT invent a heading; the group uses the raw key string so nothing is hidden.
export const PRACTICAL_SLOT_FALLBACK_HEADING: Record<string, string> = {
  situation: "Vaše situace",
  applies: "Co se na vás vztahuje",
  obligations: "Vaše povinnosti a lhůty",
  steps: "Jak postupovat",
  where: "Kam se obrátit",
  risks: "Rizika a sankce",
  gaps: "Co se nepodařilo spolehlivě doložit",
};

// The DRAFT badge copy (advocate-review-pending, G14).
export const PRACTICAL_DRAFT_BADGE = "orientační — ověřte";

// Defensive accessor: pull the `practical` block off the AnswerResponse without
// widening the shared api.ts contract. Returns null when absent or when it carries
// NOTHING renderable (no items, no gaps, no demotions) so callers render today's
// essay view byte-identically.
export function getPracticalBlock(answer: AnswerResponse): PracticalBlock | null {
  const raw = (answer as AnswerWithPractical).practical;
  if (!raw || typeof raw !== "object") return null;
  const block = raw;
  const hasItems = Array.isArray(block.items) && block.items.length > 0;
  const hasGaps = Array.isArray(block.gaps) && block.gaps.length > 0;
  const hasDemotions =
    Array.isArray(block.demoted_slots) && block.demoted_slots.length > 0;
  if (!hasItems && !hasGaps && !hasDemotions) return null;
  return block;
}

// Turn an item's grounded `provision_id` into a citation chip for the ChipRow /
// ChipResolver. Only a grounded item with a provision_id yields a chip (an
// un-grounded item asserts no citation). The chip label is left to the resolver
// (it deep-links by provision_id + corpus); we do not fabricate a label here.
export function itemChip(item: PracticalItem): AnswerChip | null {
  const pid = (item.provision_id ?? "").trim();
  if (!pid || item.grounded === false) return null;
  return { provision_id: pid };
}

// A slot key → its heading (server has none; we use the known-slot fallback, else
// the raw key so an unknown slot is labelled, never hidden).
export function slotHeading(slot: PracticalSlotKind): string {
  return PRACTICAL_SLOT_FALLBACK_HEADING[slot] || String(slot);
}

// Group items by their slot, in canonical slot order (unknown slots appended in
// first-seen order). An item with no slot is bucketed under a stable "" key so it
// is still surfaced (labelled with the empty-key fallback = the raw "" → rendered
// under a neutral heading by the caller). Never drops an item.
export interface PracticalSlotGroup {
  slot: PracticalSlotKind;
  heading: string;
  items: PracticalItem[];
}

export function groupItemsBySlot(items: PracticalItem[]): PracticalSlotGroup[] {
  const buckets = new Map<string, PracticalItem[]>();
  const seenOrder: string[] = [];
  for (const it of items) {
    if (!it) continue;
    const key = (it.slot ?? "").trim();
    if (!buckets.has(key)) {
      buckets.set(key, []);
      seenOrder.push(key);
    }
    buckets.get(key)!.push(it);
  }
  // canonical order first, then any unknown keys in first-seen order.
  const canonical = PRACTICAL_SLOT_ORDER.filter((k) => buckets.has(k));
  const unknown = seenOrder.filter((k) => !PRACTICAL_SLOT_ORDER.includes(k));
  const order = [...canonical, ...unknown];
  return order.map((slot) => ({
    slot,
    heading: slotHeading(slot),
    items: buckets.get(slot) ?? [],
  }));
}

// The best display line for a gap: first non-empty text-ish key, else the
// class/status label, else empty (the caller skips a fully-empty gap).
export function gapText(gap: PracticalGap): string {
  const candidates = [gap.text, gap.label, gap.note, gap.message];
  for (const c of candidates) {
    if (typeof c === "string" && c.trim()) return c.trim();
  }
  const cls = (gap.gap_class ?? gap.status ?? "").toString().trim();
  return cls;
}
