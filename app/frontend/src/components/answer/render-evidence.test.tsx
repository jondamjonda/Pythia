// VL-C-10 — RUNTIME render evidence for the AQ answer surface.
//
// The 7-slot practical block (PracticalPlan), the UnderstandingPanel, the clarify
// frame (ClarifyForm) and the authorities block are all type-checked elsewhere, but
// until now nothing drove them through an actual DOM render with a LIVE-SHAPE payload.
// This suite closes that gap: it renders each component from a fixture shaped exactly
// like the backend wire, and asserts the HARD INVARIANTS at RUNTIME (not just via tsc):
//
//   1. the 7-slot PracticalPlan renders every present slot group + the honest gap lines;
//   2. an UNKNOWN slot / kind falls back gracefully — no crash, a safe render labelled
//      with the raw slot key (asserted in the DOM, not inferred from types);
//   3. an XSS fixture (a slot value / gap text carrying `<img src=x onerror=…>` /
//      `<script>`) is rendered as INERT TEXT — the markup appears as an escaped string,
//      never as a live <img>/<script> node injected into the DOM;
//   4. the UnderstandingPanel, the clarify frame, and the authorities block each render
//      from a live-shape fixture without throwing.
import { describe, expect, test } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AnswerView } from "./AnswerView";
import { PracticalPlan } from "./PracticalPlan";
import { UnderstandingPanel } from "./UnderstandingPanel";
import type { AnswerResponse } from "@/lib/api";
import type { AnswerWithPractical } from "./practical";

// ── XSS fixtures ──────────────────────────────────────────────────────────────
// The two classic injection payloads. If the render layer ever used
// dangerouslySetInnerHTML (or any HTML/markdown parse) these would materialise as a
// live <img onerror> / <script> element; because every string flows through React as
// a text child, they must appear ONLY as inert, escaped text.
const XSS_IMG = '<img src=x onerror="window.__xss_fired=true">';
const XSS_SCRIPT = '<script>window.__xss_script=true</script>';

// A LIVE-SHAPE `practical` sidecar exactly as the backend PracticalBlockOut shapes it:
// a FLAT { items, gaps, demoted_slots, profile_used } block. `items` cover all seven
// known slots PLUS one UNKNOWN slot ("teleport") to exercise the unknown-slot fallback,
// and one item's value_raw carries the XSS <img> payload. `gaps` carry the honest DRAFT
// non-claims incl. one whose text carries the XSS <script> payload.
const PRACTICAL: AnswerWithPractical = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    {
      kind: "assessment",
      heading: "Hodnocení",
      text: "Jako OSVČ v Praze máte registrační a odvodové povinnosti.",
      chips: [{ provision_id: "p-1", label: "[586/1992 § 38]", corpus: "pythia" }],
    },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
  practical: {
    profile_used: true,
    items: [
      {
        slot: "situation",
        fact_type: "Situace",
        value_raw: "OSVČ, hlavní činnost, sídlo v Praze.",
        grounded: true,
      },
      {
        slot: "applies",
        fact_type: "Vztahuje se",
        value_raw: "Zákon o daních z příjmů se na vás vztahuje.",
        provision_id: "p-applies",
        grounded: true,
      },
      {
        slot: "obligations",
        fact_type: "Povinnost",
        // XSS fixture rides a grounded fact's verbatim excerpt.
        value_raw: `Podat přiznání do 1. dubna. ${XSS_IMG}`,
        provision_id: "p-oblig",
        grounded: true,
      },
      {
        slot: "steps",
        fact_type: "Postup",
        value_raw: "1) Zaregistrujte se. 2) Podejte přiznání.",
        grounded: true,
      },
      {
        slot: "where",
        fact_type: "Kam",
        value_raw: "Finanční úřad podle místa podnikání.",
        grounded: true,
      },
      {
        slot: "risks",
        fact_type: "Riziko",
        value_raw: "Pokuta za pozdní podání.",
        provision_id: "p-risk",
        grounded: true,
      },
      // UNKNOWN slot key — must NOT crash and must render under a group labelled with
      // the raw key ("teleport"), never dropped, never a fabricated heading.
      {
        slot: "teleport",
        fact_type: "Neznámý slot",
        value_raw: "Údaj z novějšího serveru, který FE ještě nezná.",
        grounded: true,
      },
    ],
    gaps: [
      { gap_class: "missing_provision", status: "draft", text: "Přesnou lhůtu se nepodařilo doložit." },
      // XSS fixture rides a gap display line.
      { gap_class: "unverified", status: "draft", text: `Neověřený údaj ${XSS_SCRIPT}` },
    ],
    demoted_slots: ["risks"],
  },
};

// A LIVE-SHAPE T7 intake block + a consented profile suggestion (drives the
// UnderstandingPanel). Facts/sub-questions/conflicts/dropped all populated so every
// sub-section renders. No profileId in scope → the write-back affordance is disabled.
const WITH_INTAKE: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    { kind: "assessment", heading: "Hodnocení", text: "Máte tyto povinnosti…", chips: [] },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
  intake: {
    sub_questions: ["Jaké máte registrační povinnosti?", "Jaké platíte odvody?"],
    facts: [
      { field: "legal_form", value: "OSVČ", source: "det" },
      { field: "municipality_code", value: "Praha", source: "llm" },
    ],
    conflicts: [{ field: "headcount_band", values: ["0", "1-5"] }],
    sufficiency: "dostatek údajů",
    dropped: [{ field: "turnover_band", reason: "nízká jistota" }],
  },
  profile_suggestions: [{ field: "legal_form", value: "OSVČ" }],
};

// A LIVE-SHAPE clarify frame (render_mode="clarify", refused=false) with the ≤3
// questions + disclaimer, exactly as answer.py shapes it.
const CLARIFY: AnswerResponse = {
  render_mode: "clarify",
  refused: false,
  band: "clarify",
  title: "Potřebujeme upřesnit několik údajů",
  disclaimer: "Toto je informace, nikoli právní rada.",
  questions: [
    { slot: "is_employer", text: "Zaměstnáváte zaměstnance?" },
    { slot: "region", text: "Ve kterém kraji má firma sídlo?" },
  ],
  matter_type: "employment",
};

// A LIVE-SHAPE authorities directory (W4 / T13).
const WITH_AUTHORITIES: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    { kind: "assessment", heading: "Hodnocení", text: "Registrujte se u úřadu.", chips: [] },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
  authorities: [
    {
      authority_type: "financni_urad",
      name: "Finanční úřad pro hlavní město Prahu",
      match_kind: "kraj",
      is_draft: true,
      source_uri: "https://www.financnisprava.cz/urady/praha",
      fetched_at: "2026-07-03",
    },
  ],
};

describe("VL-C-10 — runtime render evidence: 7-slot PracticalPlan", () => {
  test("renders each present slot group + the honest DRAFT gap lines", () => {
    render(<PracticalPlan answer={PRACTICAL} />);

    // The sidecar section header renders.
    expect(screen.getByRole("region", { name: "Praktické údaje" })).toBeInTheDocument();

    // Each present KNOWN slot renders its CZ fallback heading (situation…risks).
    for (const heading of [
      "Vaše situace",
      "Co se na vás vztahuje",
      "Vaše povinnosti a lhůty",
      "Jak postupovat",
      "Kam se obrátit",
      "Rizika a sankce",
    ]) {
      expect(screen.getByText(heading)).toBeInTheDocument();
    }

    // A grounded fact value renders as text.
    expect(screen.getByText(/Zákon o daních z příjmů se na vás vztahuje/)).toBeInTheDocument();

    // The honest gap lines render, each badged DRAFT ("orientační — ověřte"). There is
    // the gaps-group header badge + one badge per gap line → ≥ 2 badges present.
    expect(screen.getByText("Přesnou lhůtu se nepodařilo doložit.")).toBeInTheDocument();
    expect(screen.getAllByText(/orientační — ověřte/).length).toBeGreaterThanOrEqual(2);

    // The demotion caution surfaces the demoted slot ("risks" → "Rizika a sankce"),
    // never silently hidden.
    expect(
      screen.getByText(/nepodařilo spolehlivě doložit a byly potlačeny/),
    ).toBeInTheDocument();
  });

  test("an UNKNOWN slot falls back gracefully — no crash, labelled with the raw key", () => {
    // Renders without throwing (the render() above would already throw on a crash; we
    // assert the fallback render explicitly here).
    render(<PracticalPlan answer={PRACTICAL} />);

    // The unknown slot "teleport" is NOT dropped: its fact value renders …
    expect(
      screen.getByText("Údaj z novějšího serveru, který FE ještě nezná."),
    ).toBeInTheDocument();
    // … under a group heading that is the RAW slot key (no fabricated CZ heading exists
    // for an unknown slot). slotHeading() falls back to String(slot) = "teleport".
    expect(screen.getByText("teleport")).toBeInTheDocument();
  });

  test("XSS in a slot value / gap text is INERT — escaped text, no live node injected", () => {
    // Clear any residual flags, then render the payload carrying both XSS fixtures.
    delete (window as unknown as Record<string, unknown>).__xss_fired;
    delete (window as unknown as Record<string, unknown>).__xss_script;

    const { container } = render(<PracticalPlan answer={PRACTICAL} />);

    // (a) NO live element was injected: the onerror <img> and the <script> never became
    //     real DOM nodes. If React had used dangerouslySetInnerHTML these would exist.
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();

    // (b) The payload strings are present in the DOM as INERT TEXT — the raw markup is
    //     rendered verbatim (React escaped the angle-brackets into text). getByText
    //     matches a text node, proving the "<img …>" is text, not parsed HTML.
    expect(screen.getByText(/Podat přiznání do 1\. dubna\./)).toHaveTextContent(XSS_IMG);
    expect(screen.getByText(/Neověřený údaj/)).toHaveTextContent(XSS_SCRIPT);

    // (c) The serialized HTML contains the ESCAPED entity, never a live tag. This is the
    //     load-bearing inertness assertion: `<img` / `<script` must NOT appear as real
    //     markup; they must appear escaped as `&lt;img` / `&lt;script`.
    const html = container.innerHTML;
    expect(html).toContain("&lt;img");
    expect(html).toContain("&lt;script");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script");

    // (d) The onerror handler never ran and no script executed (flags stay unset).
    expect((window as unknown as Record<string, unknown>).__xss_fired).toBeUndefined();
    expect((window as unknown as Record<string, unknown>).__xss_script).toBeUndefined();
  });
});

describe("VL-C-10 — runtime render evidence: panels render from live-shape fixtures", () => {
  test("UnderstandingPanel renders (expanded) without throwing", async () => {
    const user = userEvent.setup();
    render(<UnderstandingPanel answer={WITH_INTAKE} />);

    // The collapsed panel toggle renders …
    const toggle = screen.getByRole("button", { name: /Jak jsme otázce porozuměli/ });
    expect(toggle).toBeInTheDocument();

    // … and expands to show the whitelisted intake content (sub-questions + facts).
    await user.click(toggle);
    expect(screen.getByText("Jaké máte registrační povinnosti?")).toBeInTheDocument();
    // "Praha" appears only in the extracted facts → a unique intake-fact assertion.
    expect(screen.getByText("Praha")).toBeInTheDocument();
    // "OSVČ" appears in BOTH the facts list AND the write-back suggestion chip → ≥ 1.
    expect(screen.getAllByText("OSVČ").length).toBeGreaterThanOrEqual(1);
    // No profileId in scope → the write-back save is disabled with the honest note.
    expect(
      screen.getByText(/bude dostupné po propojení odpovědi s vaším profilem/),
    ).toBeInTheDocument();
  });

  test("the clarify frame renders its questions (not the refuse UI)", () => {
    render(<AnswerView answer={CLARIFY} query="Musím platit odvody?" />);
    expect(screen.getByText("Zaměstnáváte zaměstnance?")).toBeInTheDocument();
    expect(screen.getByText("Ve kterém kraji má firma sídlo?")).toBeInTheDocument();
    // A clarify is NOT a refusal.
    expect(screen.queryByText(/nemůžeme spolehlivě odpovědět/i)).not.toBeInTheDocument();
  });

  test("the authorities block renders the labeled non-legal directory", () => {
    render(<AnswerView answer={WITH_AUTHORITIES} />);
    const region = screen.getByRole("region", { name: "Kam se obrátit" });
    expect(within(region).getByText("Finanční úřad pro hlavní město Prahu")).toBeInTheDocument();
    expect(within(region).getByText(/není právní obsah/i)).toBeInTheDocument();
  });
});
