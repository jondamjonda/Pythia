// Global test setup — registers the @testing-library/jest-dom custom matchers
// (toBeInTheDocument, toHaveValue, …) on vitest's expect, and auto-cleans the
// rendered DOM between tests.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
