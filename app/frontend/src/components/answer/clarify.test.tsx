// Component test for the E12 clarify round-trip (Track A / VL-A).
//
// What this guards (the FE mirror of the backend clarify frame, render_mode="clarify"):
//   1. A `render_mode="clarify"` response renders the slot-collection FORM — the ≤3
//      catalog `questions` appear + the information-not-advice disclaimer — NOT the
//      refuse / non-answer UI (a clarify is refused=false, never a dead-end).
//   2. Filling every question + submitting RE-POSTs the SAME query with the collected
//      answers as `slots` — asserted on the mocked api.answer call's second argument.
//   3. The mocked SECOND (sound) response then renders IN PLACE (the harness swaps the
//      answer via onAnswer, exactly as the answer page does).
import { afterEach, describe, expect, test, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { AnswerView } from "./AnswerView";
import type { AnswerResponse } from "@/lib/api";

// Mock the API module: only api.answer is used by the round-trip. The mock is reset per
// test and its per-test behaviour is set with mockResolvedValueOnce.
const answerMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { answer: (...args: unknown[]) => answerMock(...args) },
}));

// A clarify frame exactly as src/regsuper/api/answer.py shapes it: render_mode="clarify",
// refused=false, band="clarify", ≤3 { slot, text } questions + the constant disclaimer.
const CLARIFY: AnswerResponse = {
  render_mode: "clarify",
  refused: false,
  band: "clarify",
  title: "Potřebujeme upřesnit několik údajů",
  body: "Abychom odpověděli přesně, potřebujeme upřesnit několik údajů.",
  disclaimer: "Toto je informace, nikoli právní rada.",
  questions: [
    { slot: "is_employer", text: "Zaměstnáváte zaměstnance?" },
    { slot: "legal_form", text: "V jaké právní formě podnikáte?" },
    { slot: "region", text: "Ve kterém kraji má firma sídlo?" },
  ],
  matter_type: "employment",
};

// The SOUND second-turn answer (render_mode="full") returned after the follow-up.
const SOUND: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    {
      kind: "assessment",
      heading: "Hodnocení",
      text: "Jako zaměstnavatel s.r.o. v Praze máte tyto povinnosti…",
      chips: [],
    },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
};

// A tiny host that mirrors the answer page: it owns the displayed answer and passes
// setAns as onAnswer, so the SECOND response renders in place (round-trip closed).
function Host({ initial, query }: { initial: AnswerResponse; query: string }) {
  const [ans, setAns] = useState<AnswerResponse>(initial);
  return <AnswerView answer={ans} query={query} onAnswer={setAns} />;
}

afterEach(() => {
  answerMock.mockReset();
});

describe("clarify round-trip UI (E12)", () => {
  test("clarify frame renders the questions as a form, not the refuse UI", () => {
    render(<Host initial={CLARIFY} query="Musím platit odvody za zaměstnance?" />);

    // The three catalog questions appear (as form labels).
    expect(screen.getByText("Zaměstnáváte zaměstnance?")).toBeInTheDocument();
    expect(screen.getByText("V jaké právní formě podnikáte?")).toBeInTheDocument();
    expect(screen.getByText("Ve kterém kraji má firma sídlo?")).toBeInTheDocument();
    // The information-not-advice disclaimer is present.
    expect(screen.getByText("Toto je informace, nikoli právní rada.")).toBeInTheDocument();
    // It is a FORM with a submit button — NOT the refuse / non-answer banner.
    expect(
      screen.getByRole("button", { name: /Odpovědět a pokračovat/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/nemůžeme spolehlivě odpovědět/i),
    ).not.toBeInTheDocument();
  });

  test("filling + submitting re-POSTs the SAME query with slots; second answer renders in place", async () => {
    const user = userEvent.setup();
    answerMock.mockResolvedValueOnce(SOUND);
    const query = "Musím platit odvody za zaměstnance?";
    render(<Host initial={CLARIFY} query={query} />);

    // Answer the two enum (select) questions + the free-text (region) question.
    await user.selectOptions(screen.getByLabelText("Zaměstnáváte zaměstnance?"), "yes");
    await user.selectOptions(screen.getByLabelText("V jaké právní formě podnikáte?"), "sro");
    await user.type(screen.getByLabelText("Ve kterém kraji má firma sídlo?"), "Praha");

    await user.click(
      screen.getByRole("button", { name: /Odpovědět a pokračovat/i }),
    );

    // The follow-up POSTed the SAME query + the collected answers as `slots`.
    await waitFor(() => expect(answerMock).toHaveBeenCalledTimes(1));
    const [sentQuery, opts] = answerMock.mock.calls[0] as [string, { slots?: Record<string, string> }];
    expect(sentQuery).toBe(query);
    expect(opts.slots).toEqual({
      is_employer: "yes",
      legal_form: "sro",
      region: "Praha",
    });

    // The SECOND (sound) response renders IN PLACE — the clarify form is gone.
    const section = await screen.findByText(/Jako zaměstnavatel s\.r\.o\. v Praze/);
    expect(section).toBeInTheDocument();
    expect(screen.queryByText("Zaměstnáváte zaměstnance?")).not.toBeInTheDocument();
  });

  test("submit is disabled until every question is answered", async () => {
    const user = userEvent.setup();
    render(<Host initial={CLARIFY} query="q" />);
    const submit = screen.getByRole("button", { name: /Odpovědět a pokračovat/i });
    expect(submit).toBeDisabled();

    // Answer only two of three → still disabled; no POST fired.
    await user.selectOptions(screen.getByLabelText("Zaměstnáváte zaměstnance?"), "no");
    await user.selectOptions(screen.getByLabelText("V jaké právní formě podnikáte?"), "as");
    expect(submit).toBeDisabled();
    expect(answerMock).not.toHaveBeenCalled();
  });

  test("a clarify with an out-of-enum slot renders a free-text input (region)", () => {
    render(<Host initial={CLARIFY} query="q" />);
    // region is not in SLOT_OPTIONS → a text input, not a select.
    const region = screen.getByLabelText("Ve kterém kraji má firma sídlo?");
    expect(region).toHaveProperty("tagName", "INPUT");
    // the two enum slots ARE selects.
    const employer = within(document.body).getByLabelText("Zaměstnáváte zaměstnance?");
    expect(employer).toHaveProperty("tagName", "SELECT");
  });
});
