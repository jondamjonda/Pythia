// Component test for the T7 / VL-A-11 profile write-back (A3.5) on the answer view.
//
// What this guards (the FE mirror of the consented write-back suggestions):
//   1. A full answer carrying an `intake` block + `profile_suggestions` renders the
//      "Uložit do profilu" write-back affordance inside the (expanded) Understanding
//      Panel, listing each suggested field.
//   2. With a `profileId` in scope, clicking "Uložit do profilu" PATCHes that profile
//      with the mapped body — and the body ALWAYS carries `suggested_only: true` so the
//      server-side only-fill-EMPTY guard engages (defence-in-depth; a suggestion never
//      overwrites an existing value).
//   3. Without a `profileId` the save button is disabled (the write-back is deferred
//      with an honest note) and NO PATCH fires.
import { afterEach, describe, expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AnswerView } from "./AnswerView";
import type { AnswerResponse } from "@/lib/api";

// Mock the API module: only api.patchProfile is used by the write-back. Reset per test.
const patchMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { patchProfile: (...args: unknown[]) => patchMock(...args) },
}));

// A full answer with a T7 intake block (so the panel renders) + consented write-back
// suggestions (the asker's own whitelisted facts).
const WITH_SUGGESTIONS: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    {
      kind: "assessment",
      heading: "Hodnocení",
      text: "Odpověď na vaši otázku.",
      chips: [],
    },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
  intake: {
    facts: [{ field: "nace_code", value: "62.01", source: "llm" }],
    sufficiency: "dostatek údajů",
  },
  profile_suggestions: [
    { field: "nace_code", value: "62.01" },
    { field: "legal_form", value: "sro" },
  ],
};

afterEach(() => {
  patchMock.mockReset();
});

describe("profile write-back (T7 / VL-A-11) on the answer view", () => {
  test("suggestions render + a click PATCHes the profile with suggested_only guard", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValueOnce({ id: "prof-1" });
    render(<AnswerView answer={WITH_SUGGESTIONS} profileId="prof-1" />);

    // Expand the Understanding Panel to reveal the write-back affordance.
    await user.click(screen.getByRole("button", { name: /Jak jsme otázce porozuměli/i }));

    // Scope to the write-back section (the "Uložit do profilu" save button's section) —
    // "Obor (NACE)" also appears in the extracted-facts list, so we assert within.
    const saveBtn = screen.getByRole("button", { name: /^Uložit do profilu$/i });
    const writeback = saveBtn.closest("section");
    expect(writeback).not.toBeNull();
    // The suggested fields render as pills inside the write-back section.
    expect(within(writeback as HTMLElement).getByText("Obor (NACE)")).toBeInTheDocument();
    expect(within(writeback as HTMLElement).getByText("Právní forma")).toBeInTheDocument();

    // Click the one-click save.
    await user.click(saveBtn);

    // The PATCH fired against the linked profile with the mapped body + the guard flag.
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [id, body] = patchMock.mock.calls[0] as [string, Record<string, unknown>];
    expect(id).toBe("prof-1");
    expect(body.nace_code).toBe("62.01");
    expect(body.legal_form).toBe("sro");
    // The server-side only-fill-EMPTY guard is always opted into.
    expect(body.suggested_only).toBe(true);
  });

  test("with no profileId the save is disabled and no PATCH fires", async () => {
    const user = userEvent.setup();
    render(<AnswerView answer={WITH_SUGGESTIONS} profileId={null} />);

    await user.click(screen.getByRole("button", { name: /Jak jsme otázce porozuměli/i }));

    const save = screen.getByRole("button", { name: /^Uložit do profilu$/i });
    expect(save).toBeDisabled();
    await user.click(save);
    expect(patchMock).not.toHaveBeenCalled();
  });
});
