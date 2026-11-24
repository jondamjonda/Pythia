// Component test for the W4 / T13 "Kam se obrátit" authorities directory block.
//
// What this guards (the FE mirror of the backend `authorities` list on AnswerResponse):
//   1. A payload carrying `authorities` renders the LABELED "Kam se obrátit" block with
//      a clear NON-LEGAL label, each office's name, its source link, and the `stav k
//      {fetched_at}` as-of date.
//   2. It is a directory aid, NOT a citation — it is NOT rendered inside the grounded
//      "Podkladová ustanovení" (citations) list.
//   3. An empty (`[]`) or absent (`undefined`) `authorities` renders NO block at all
//      (no header, no placeholder) → the view is byte-identical to today.
import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { AnswerView } from "./AnswerView";
import type { AnswerResponse } from "@/lib/api";

// A full answer frame with a minimal grounded section (so the citations list renders)
// PLUS a resolved authorities directory.
const WITH_AUTHORITIES: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    {
      kind: "assessment",
      heading: "Hodnocení",
      text: "Registrujte se u příslušného finančního úřadu.",
      chips: [
        { provision_id: "p-1", label: "[586/1992 § 38]", corpus: "pythia" },
      ],
    },
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
    {
      authority_type: "cssz",
      name: "Česká správa sociálního zabezpečení",
      match_kind: "national",
      is_draft: true,
    },
  ],
};

const NO_AUTHORITIES: AnswerResponse = {
  render_mode: "full",
  refused: false,
  band: "sound",
  sections: [
    {
      kind: "assessment",
      heading: "Hodnocení",
      text: "Registrujte se u příslušného finančního úřadu.",
      chips: [],
    },
  ],
  disclaimer: "Toto je informace, nikoli právní rada.",
  // authorities intentionally absent.
};

describe("AuthoritiesBlock — 'Kam se obrátit' directory", () => {
  test("renders the labeled non-legal block with each office + the 'stav k' as-of date", () => {
    render(<AnswerView answer={WITH_AUTHORITIES} />);

    // The labeled directory block header + the explicit NON-LEGAL label are present.
    expect(screen.getByRole("region", { name: "Kam se obrátit" })).toBeInTheDocument();
    expect(screen.getByText("Kam se obrátit")).toBeInTheDocument();
    expect(
      screen.getByText(/není právní obsah/i),
    ).toBeInTheDocument();

    // Each resolved office renders by name.
    expect(
      screen.getByText("Finanční úřad pro hlavní město Prahu"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Česká správa sociálního zabezpečení"),
    ).toBeInTheDocument();

    // The `stav k {fetched_at}` as-of note renders for the entry that carries a date.
    expect(screen.getByText("stav k 2026-07-03")).toBeInTheDocument();

    // The source link renders (host as label), pointing at the directory URL.
    const src = screen.getByRole("link", { name: /Zdroj:/ });
    expect(src).toHaveAttribute(
      "href",
      "https://www.financnisprava.cz/urady/praha",
    );

    // NON-LEGAL SEPARATION: the office name is NOT inside the citations
    // ("Podkladová ustanovení") list — that block lists only provision chips.
    const provisions = screen.getByText("Podkladová ustanovení").closest("section");
    expect(provisions).not.toBeNull();
    expect(provisions).not.toHaveTextContent("Finanční úřad pro hlavní město Prahu");
  });

  test("renders NO block when authorities is absent", () => {
    render(<AnswerView answer={NO_AUTHORITIES} />);
    expect(screen.queryByText("Kam se obrátit")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Kam se obrátit" }),
    ).not.toBeInTheDocument();
  });

  test("renders NO block when authorities is an empty list", () => {
    render(<AnswerView answer={{ ...NO_AUTHORITIES, authorities: [] }} />);
    expect(screen.queryByText("Kam se obrátit")).not.toBeInTheDocument();
  });
});
