// Vitest runner for the frontend co-located unit + component tests.
//
// - environment: jsdom → React components + @testing-library render in a DOM.
// - react() → JSX/TSX transform (matches the app's react-jsx runtime).
// - resolve.tsconfigPaths → resolves the "@/*" alias (understanding.ts imports a
//   TYPE from "@/lib/api"; the clarify test imports "@/lib/api" + the components).
//   Native Vite support (v4), so no vite-tsconfig-paths plugin needed.
// - setupFiles → registers @testing-library/jest-dom matchers.
//
// `npm test` = `vitest run` (single pass, CI-style). Discovers *.test.ts(x) under src/.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: { tsconfigPaths: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // The [...path]/route.test.ts is a Next.js server-route test written for the
    // Node built-in runner (node --test); it is not a vitest suite. Exclude it so
    // `vitest run` does not try to execute it as a vitest file.
    exclude: [
      "node_modules/**",
      "src/app/control/api/**/route.test.ts",
    ],
  },
});
