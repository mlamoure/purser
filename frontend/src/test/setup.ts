/* Runs before every test file (vitest.config.ts → setupFiles). */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { resetApiMock } from "./api";

// Swap the API client for the route-table mock in ./api in EVERY test file;
// the module's other exports (errorDetail, setOnUnauthorized) stay real.
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  api: (await import("./api")).apiMock,
}));

afterEach(() => {
  // Unmount explicitly: Testing Library only auto-cleans when the test
  // runner's globals are enabled, and this config keeps them off.
  cleanup();
  const missed = resetApiMock();
  if (missed.length > 0) {
    throw new Error(`API calls without a mockApi() route: ${missed.join(", ")}`);
  }
});
