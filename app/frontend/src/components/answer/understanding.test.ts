// Co-located FE test for the T7 UNDERSTANDING PANEL pure logic (understanding.ts).
//
// What this guards:
//   1. hasContent() gate — an ABSENT / empty intake block ⇒ false ⇒ the panel renders
//      nothing ⇒ AnswerView renders today's essay view byte-identically. A block with
//      ANY renderable field ⇒ true (the panel renders from the fixture).
//   2. buildPatch() — the consented profile write-back maps the whitelisted suggestion
//      fields to the profiles PATCH body: compliance gates → `characteristics` (coerced
//      to booleans), scoping fields → their ProfilePatch keys; an unknown field is
//      dropped (never sent).
//   3. conflictText() / sourceLabel() / fieldLabel() defensive display helpers.
//
// Runner: vitest (`npm test`). understanding.ts imports only a *type* from @/lib/api;
// vitest resolves the "@/*" alias via vite-tsconfig-paths, so a plain static import loads
// the pure sibling. Run from `frontend/`:  npm test
import { test, expect } from "vitest";
import {
  buildPatch,
  conflictText,
  fieldLabel,
  hasContent,
  sourceLabel,
} from "./understanding";

// --- 1. hasContent gate: absent / empty ⇒ false (byte-identical essay view) ---------
test("hasContent: null / undefined / empty block ⇒ false", () => {
  expect(hasContent(null)).toBe(false);
  expect(hasContent(undefined)).toBe(false);
  expect(hasContent({})).toBe(false);
  expect(
    hasContent({ sub_questions: [], facts: [], conflicts: [], dropped: [], sufficiency: "" }),
  ).toBe(false);
});

test("hasContent: any renderable field ⇒ true (panel renders from the fixture)", () => {
  expect(hasContent({ sub_questions: ["Jaká je právní forma?"] })).toBe(true);
  expect(hasContent({ facts: [{ field: "legal_form", value: "sro" }] })).toBe(true);
  expect(hasContent({ conflicts: [{ field: "headcount_band", values: ["5", "12"] }] })).toBe(true);
  expect(hasContent({ sufficiency: "sufficient" })).toBe(true);
  expect(hasContent({ dropped: [{ field: "turnover_band" }] })).toBe(true);
});

// --- 2. buildPatch: consented profile write-back mapping ----------------------------
test("buildPatch: gates → characteristics (coerced), scoping → ProfilePatch keys", () => {
  const { body, count } = buildPatch([
    { field: "legal_form", value: "sro" },
    { field: "nace_code", value: "62.01" },
    { field: "jurisdiction", value: "CZ" },
    { field: "processes_personal_data", value: "true" },
    { field: "is_employer", value: "ano" },
  ]);
  expect(count).toBe(5);
  expect(body.legal_form).toBe("sro");
  expect(body.nace_code).toBe("62.01");
  expect(body.primary_jurisdiction).toBe("CZ"); // jurisdiction → primary_jurisdiction
  const chars = body.characteristics as Record<string, boolean>;
  expect(chars.processes_personal_data).toBe(true);
  expect(chars.is_employer).toBe(true); // "ano" is truthy
  // VL-A-11 / A3.5 — the body ALWAYS opts into the server-side only-fill-EMPTY guard.
  expect(body.suggested_only).toBe(true);
});

test("buildPatch: unknown / query-shape fields are dropped (never sent)", () => {
  const { body, count } = buildPatch([
    { field: "obligation_type", value: "registration" }, // query-shape, not a profile column
    { field: "free_text_note", value: "junk" }, // not whitelisted anywhere
  ]);
  expect(count).toBe(0);
  // Nothing whitelisted was folded in — only the guard flag rides the body.
  expect(body).toEqual({ suggested_only: true });
});

test("buildPatch: empty-value suggestions are skipped", () => {
  const { count } = buildPatch([{ field: "legal_form", value: "  " }]);
  expect(count).toBe(0);
});

// --- 3. display helpers -------------------------------------------------------------
test("sourceLabel: known sources mapped, unknown passthrough, empty ⇒ null", () => {
  expect(sourceLabel("det")).toBe("z pravidel");
  expect(sourceLabel("llm")).toBe("z textu");
  expect(sourceLabel("profile")).toBe("z profilu");
  expect(sourceLabel("other")).toBe("other");
  expect(sourceLabel("")).toBe(null);
  expect(sourceLabel(null)).toBe(null);
});

test("fieldLabel: known field → CZ label, unknown → raw key", () => {
  expect(fieldLabel("legal_form")).toBe("Právní forma");
  expect(fieldLabel("mystery")).toBe("mystery");
});

test("conflictText: field label + candidate values, no free-text passes through", () => {
  expect(conflictText({ field: "headcount_band", values: ["5", "12"] })).toBe(
    "Počet zaměstnanců: 5 / 12",
  );
  expect(conflictText({ field: "legal_form" })).toBe("Právní forma"); // no values ⇒ label alone
  // a stray free-text key on the raw object is ignored (only field + values are read)
  expect(
    conflictText({ field: "headcount_band", values: ["5"], detail: "junk" } as never),
  ).toBe("Počet zaměstnanců: 5");
  expect(conflictText({ field: "" })).toBe(""); // no field ⇒ empty
  expect(conflictText({} as never)).toBe("");
});
