// AQ-05 — component test for the municipality autocomplete on the profile form.
//
// What this guards:
//   1. Typing a prefix (debounced) queries GET /api/municipalities and renders
//      the returned matches as options (fetch is mocked).
//   2. Selecting a match stores the REAL `code` via onSelect (never free text)
//      and shows the picked name back in the box.
//   3. An empty/too-short query issues NO request (degrades to no suggestions).
//   4. A failed fetch degrades gracefully — no options, no throw, field usable.
import { afterEach, describe, expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MunicipalityAutocomplete } from "./municipality-autocomplete";
import type { MunicipalityMatch } from "@/lib/api";

// Mock the API module: only api.searchMunicipalities is used. Reset per test.
const searchMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { searchMunicipalities: (...args: unknown[]) => searchMock(...args) },
}));

const PLZEN: MunicipalityMatch = {
  name: "Plzeň",
  code: "554791",
  okres: "Plzeň-město",
  orp: "Plzeň",
  kraj: "Plzeňský kraj",
};
const PLASY: MunicipalityMatch = {
  name: "Plasy",
  code: "500011",
  okres: "Plzeň-sever",
  orp: "Kaznějov",
  kraj: "Plzeňský kraj",
};

afterEach(() => {
  searchMock.mockReset();
});

describe("MunicipalityAutocomplete", () => {
  test("typing offers matches from the search endpoint", async () => {
    const user = userEvent.setup();
    searchMock.mockResolvedValue([PLZEN, PLASY]);
    render(<MunicipalityAutocomplete value={null} onSelect={() => {}} />);

    await user.type(screen.getByRole("combobox"), "Plz");

    // The mocked search fired with the typed prefix, and matches render as options.
    await waitFor(() => expect(searchMock).toHaveBeenCalled());
    expect(searchMock.mock.calls[0][0]).toBe("Plz");
    // Both hits render (matched on the visible name span; exact text avoids the
    // "Plzeňský kraj" disambiguator colliding across rows).
    await waitFor(() => expect(screen.getByText("Plzeň")).toBeInTheDocument());
    expect(screen.getByText("Plasy")).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
  });

  test("selecting a match stores the real code (never free text)", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    searchMock.mockResolvedValue([PLZEN, PLASY]);
    render(<MunicipalityAutocomplete value={null} onSelect={onSelect} />);

    await user.type(screen.getByRole("combobox"), "Plz");
    await waitFor(() => expect(screen.getByText("Plzeň")).toBeInTheDocument());

    // Click the "Plzeň" option (via its visible name span → the option button).
    await user.click(screen.getByText("Plzeň"));

    // onSelect receives the CODE, not the name; the box shows the picked name.
    expect(onSelect).toHaveBeenLastCalledWith("554791");
    expect(screen.getByRole("combobox")).toHaveValue("Plzeň");
  });

  test("a too-short / empty query issues no request", async () => {
    const user = userEvent.setup();
    searchMock.mockResolvedValue([PLZEN]);
    render(<MunicipalityAutocomplete value={null} onSelect={() => {}} />);

    await user.type(screen.getByRole("combobox"), "P");
    // Give any (unexpected) debounced request time to fire.
    await new Promise((r) => setTimeout(r, 300));
    expect(searchMock).not.toHaveBeenCalled();
  });

  test("a failed fetch degrades gracefully — no options, no throw", async () => {
    const user = userEvent.setup();
    searchMock.mockRejectedValue(new Error("network"));
    render(<MunicipalityAutocomplete value={null} onSelect={() => {}} />);

    await user.type(screen.getByRole("combobox"), "Plz");
    await waitFor(() => expect(searchMock).toHaveBeenCalled());
    // No options rendered, and the input is still there/usable.
    expect(screen.queryByRole("option")).toBeNull();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });
});
