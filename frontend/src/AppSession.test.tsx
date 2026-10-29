import { act, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { fail, mockApi, ok } from "./test/api";
import { renderWithProviders } from "./test/render";

// The mid-session 401 hook (#220) is captured here so the test can fire it the
// way the client middleware does.
const hooks = vi.hoisted(() => ({ unauthorized: null as null | (() => void) }));

vi.mock("./lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./lib/api")>()),
  api: (await import("./test/api")).apiMock,
  setOnUnauthorized: (handler: (() => void) | null) => {
    hooks.unauthorized = handler;
  },
}));

// The session query under a key App never spelled out by hand: every reader,
// the 401 handler included, must take the key from meQuery.
vi.mock("./lib/me", async (importOriginal) => {
  const real = await importOriginal<typeof import("./lib/me")>();
  return { meQuery: { ...real.meQuery, queryKey: ["session", "v2"] } };
});

const admin = {
  user: {
    id: "u-1",
    email: "admin@example.test",
    display_name: null,
    role: "admin",
    has_password: true,
    has_oidc: false,
    created_at: "2026-01-05T12:00:00Z",
  },
  token_name: "session:password:admin@example.test",
  scopes: ["read", "write", "admin"],
  redacted_reads: false,
};

describe("App session (#220)", () => {
  it("a 401 mid-session refetches the session by meQuery's own key", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": ok(admin),
      "GET /users": ok([]),
      "GET /auth/methods": ok({ password: true, oidc: false }),
    });
    renderWithProviders(<App />, { route: "/users" });
    expect(await screen.findByRole("link", { name: "Audit Log" })).toBeInTheDocument();

    // The session expires; the next API call 401s and fires the hook.
    mockApi({ "GET /auth/me": fail(401, "not authenticated") });
    expect(hooks.unauthorized).not.toBeNull();
    await act(async () => {
      hooks.unauthorized?.();
    });
    expect(await screen.findByRole("heading", { name: "Sign in to Purser" })).toBeInTheDocument();
  });
});
