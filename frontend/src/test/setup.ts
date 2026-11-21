/* Runs before every test file (vitest.config.ts → setupFiles). */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { mockApi, ok, resetApiMock } from "./api";

// Swap the API client for the route-table mock in ./api in EVERY test file;
// the module's other exports (errorDetail, setOnUnauthorized) stay real.
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  api: (await import("./api")).apiMock,
}));

// GET /clock is read by nearly every page (#540): the household time zone
// formats every timestamp. Each test starts with this default — UTC, so
// expected timestamps do not depend on the machine's zone — and a test
// routes its own /clock to override it (mockApi replaces the key).
export const DEFAULT_CLOCK = { ledger_today: "2026-11-17", timezone: "UTC" };
beforeEach(() => {
  mockApi({ "GET /clock": ok(DEFAULT_CLOCK) });
});

afterEach(() => {
  // Unmount explicitly: Testing Library only auto-cleans when the test
  // runner's globals are enabled, and this config keeps them off.
  cleanup();
  const missed = resetApiMock();
  if (missed.length > 0) {
    throw new Error(`API calls without a mockApi() route: ${missed.join(", ")}`);
  }
});
