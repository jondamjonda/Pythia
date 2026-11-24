// understanding.ts (T7) — the PURE, defensive logic + copy for the UNDERSTANDING PANEL
// ("Jak jsme otázce porozuměli"). No React, no runtime `@/lib` import (types only) — a
// sibling of UnderstandingPanel.tsx exactly as practical.ts is a sibling of
// PracticalPlan.tsx. Kept pure so it is unit-testable under the bare Node test runner.
//
// The panel surfaces the WHITELISTED T7 `intake` block the answer payload MAY carry
// (server flag `answer_intake_panel_enabled` ON + engine intake_v2). When the payload
// carries NO block (today), the panel renders nothing and the essay view is byte-identical.
import type { IntakeBlock, IntakeConflict, ProfileSuggestion } from "@/lib/api";

// DRAFT / advocate-review-pending Czech UI copy (fixed chrome, not legal substance).
export const UNDERSTANDING_COPY = {
  panelTitle: "Jak jsme otázce porozuměli",
  panelHint:
    "Orientační přehled toho, jak systém rozuměl vaší otázce — ověřte, že sedí.",
  subQuestions: "Na co jsme se ptali",
  facts: "Co jsme z otázky vyčetli",
  conflicts: "Rozpory k ověření",
  sufficiency: "Dostatek údajů",
  dropped: "Co jsme nevzali v potaz",
  droppedHint: "Tyto údaje jsme nezahrnuli (nízká jistota nebo chybějící podklad).",
  saveTitle: "Uložit do profilu",
  saveHint:
    "Můžete tyto údaje o vašem podnikání uložit do profilu pro příště. Uloží se jen po vašem kliknutí.",
  saveBtn: "Uložit do profilu",
  saveBtnDone: "Uloženo",
  saveBtnSaving: "Ukládám…",
  saveDisabledNote:
    "Uložení do profilu bude dostupné po propojení odpovědi s vaším profilem.",
  draftBadge: "orientační — ověřte",
} as const;

// Human-readable Czech labels for the whitelisted fact/field keys. An unknown key is
// shown as its raw key (never hidden, never invented copy).
export const FIELD_LABEL: Record<string, string> = {
  legal_form: "Právní forma",
  nace_code: "Obor (NACE)",
  headcount_band: "Počet zaměstnanců",
  turnover_band: "Obrat",
  jurisdiction: "Jurisdikce",
  municipality_code: "Obec",
  preferred_language: "Jazyk",
  processes_personal_data: "Zpracovává osobní údaje",
  handles_client_money: "Nakládá s cizími penězi",
  has_real_estate: "Vlastní nemovitost",
  cross_border: "Přeshraniční činnost",
  is_employer: "Zaměstnavatel",
  sector: "Odvětví",
  actor: "Kdo se ptá",
  role: "Role",
  obligation_type: "Typ povinnosti",
};

export function fieldLabel(field: string): string {
  return FIELD_LABEL[field] ?? field;
}

// Source badge: det (deterministic) / llm / profile. An unknown/absent source → no badge.
export const SOURCE_LABEL: Record<string, string> = {
  det: "z pravidel",
  llm: "z textu",
  profile: "z profilu",
};

export function sourceLabel(source?: string | null): string | null {
  const key = (source ?? "").trim();
  if (!key) return null;
  return SOURCE_LABEL[key] ?? key;
}

// The display line for a whitelisted conflict: "<field label>: v1 / v2". The server surfaces only
// {field, values} (no free-text keys), so this renders the labelled field and its candidate values;
// a conflict with no values shows the field label alone. Unknown-shape safe.
export function conflictText(conflict: IntakeConflict): string {
  const field = typeof conflict?.field === "string" ? conflict.field.trim() : "";
  if (!field) return "";
  const values = Array.isArray(conflict?.values)
    ? conflict.values
        .filter((v) => typeof v === "string" || typeof v === "number" || typeof v === "boolean")
        .map((v) => String(v).trim())
        .filter(Boolean)
    : [];
  return values.length > 0 ? `${fieldLabel(field)}: ${values.join(" / ")}` : fieldLabel(field);
}

// True when the block carries something renderable — else the panel renders nothing
// (the essay view stays byte-identical).
export function hasContent(block: IntakeBlock | null | undefined): boolean {
  if (!block || typeof block !== "object") return false;
  return (
    (Array.isArray(block.sub_questions) && block.sub_questions.length > 0) ||
    (Array.isArray(block.facts) && block.facts.length > 0) ||
    (Array.isArray(block.conflicts) && block.conflicts.length > 0) ||
    Boolean((block.sufficiency ?? "").toString().trim()) ||
    (Array.isArray(block.dropped) && block.dropped.length > 0)
  );
}

// --- consented profile write-back mapping ---------------------------------
// The five compliance gates → `characteristics`; the scoping fields → their ProfilePatch
// keys. Never sends a field the endpoint does not own.
export const GATE_FIELDS: ReadonlySet<string> = new Set([
  "processes_personal_data",
  "handles_client_money",
  "has_real_estate",
  "cross_border",
  "is_employer",
]);

export const SCOPING_FIELD_MAP: Record<string, string> = {
  legal_form: "legal_form",
  nace_code: "nace_code",
  headcount_band: "headcount_band",
  jurisdiction: "primary_jurisdiction",
  preferred_language: "preferred_language",
  municipality_code: "municipality_code",
};

export function truthy(value: string): boolean {
  const v = value.trim().toLowerCase();
  return v === "true" || v === "1" || v === "yes" || v === "ano";
}

// Map the whitelisted profile suggestions to the profiles PATCH body. `count` is how
// many suggestions were folded in (0 → nothing to save; the caller no-ops the click).
//
// VL-A-11 / A3.5: the body ALWAYS carries `suggested_only: true` so the server-side
// only-fill-EMPTY guard engages — a suggestion may fill a field ONLY when it is
// currently empty and NEVER overwrites the SME's own standing value. The guard is
// enforced server-side (defence-in-depth); this flag opts the write-back into it.
export function buildPatch(
  suggestions: ProfileSuggestion[],
): { body: Record<string, unknown>; count: number } {
  const characteristics: Record<string, boolean> = {};
  const body: Record<string, unknown> = { suggested_only: true };
  let count = 0;
  for (const s of suggestions) {
    if (!s || !s.field) continue;
    const value = (s.value ?? "").toString();
    if (!value.trim()) continue;
    if (GATE_FIELDS.has(s.field)) {
      characteristics[s.field] = truthy(value);
      count += 1;
    } else if (SCOPING_FIELD_MAP[s.field]) {
      body[SCOPING_FIELD_MAP[s.field]] = value;
      count += 1;
    }
  }
  if (Object.keys(characteristics).length > 0) body.characteristics = characteristics;
  return { body, count };
}
