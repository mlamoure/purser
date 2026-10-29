import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "./App";
import { apiMock, fail, mockApi, ok } from "./test/api";
import { accountOut } from "./test/fixtures";
import { renderWithProviders } from "./test/render";

const session = (scopes: string[]) => ({
  user: {
    id: "u-1",
    email: "admin@example.test",
    display_name: null,
    role: scopes.includes("admin") ? "admin" : "member",
    has_password: true,
    has_oidc: false,
    created_at: "2026-01-05T12:00:00Z",
  },
  token_name: "session:password:admin@example.test",
  scopes,
  redacted_reads: false,
});

describe("App shell", () => {
  it("routes to the first-run wizard while setup is needed", async () => {
    mockApi({ "GET /auth/setup/status": ok({ setup_needed: true }) });
    renderWithProviders(<App />);

    expect(await screen.findByRole("heading", { name: "Welcome to Purser" })).toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/auth/me");
  });

  it("shows the sign-in page when there is no session", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": fail(401, "not authenticated"),
      "GET /auth/methods": ok({ password: true, oidc: false }),
    });
    renderWithProviders(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in to Purser" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("shows the Audit Log tab to an admin session", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": ok(session(["read", "write", "admin"])),
      "GET /users": ok([]),
    });
    renderWithProviders(<App />, { route: "/users" });

    expect(await screen.findByRole("link", { name: "Audit Log" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Add a user" })).toBeInTheDocument();
  });

  it("keeps the session when a page invalidates every query (#390)", async () => {
    // AccountRegister reads the session through useIsAdmin, and a register
    // action invalidates every query, which refetches "me". That refetch must
    // be App's real /auth/me call: a second observer's stub queryFn answered
    // null, and App fell back to the sign-in page.
    const account = accountOut({
      id: "acct-b",
      type: "brokerage",
      path: "assets.broker",
      holdings_source: "manual",
    });
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": ok(session(["read", "write", "admin"])),
      "GET /accounts/{account_id}": ok(account),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "GET /accounts/{account_id}/positions": ok([]),
    });
    const view = renderWithProviders(<App />, { route: "/accounts/acct-b" });
    expect(await screen.findByLabelText("Holdings source")).toBeInTheDocument();
    const meCalls = () => apiMock.GET.mock.calls.filter(([path]) => path === "/auth/me").length;
    expect(meCalls()).toBe(1);

    await act(async () => {
      await view.queryClient.invalidateQueries();
    });

    await waitFor(() => expect(meCalls()).toBe(2));
    expect(screen.queryByRole("heading", { name: "Sign in to Purser" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Holdings source")).toBeInTheDocument();
  });

  it("hides the Audit Log tab from a non-admin session", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": ok(session(["read", "write"])),
      "GET /users": ok([]),
    });
    renderWithProviders(<App />, { route: "/users" });

    expect(await screen.findByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Audit Log" })).not.toBeInTheDocument();
  });
});

describe("source offer (AGPL-3.0 section 13, #429)", () => {
  // Server routes, not SPA routes: /source redirects to PURSER_SOURCE_URL.
  const expectSourceLinks = () => {
    expect(screen.getByRole("link", { name: "Source code" })).toHaveAttribute("href", "/source");
    expect(screen.getByRole("link", { name: "Third-party licences" })).toHaveAttribute(
      "href",
      "/third-party-licenses.txt",
    );
    expect(screen.getByRole("link", { name: "GNU AGPL v3" })).toHaveAttribute(
      "href",
      "https://www.gnu.org/licenses/agpl-3.0.html",
    );
  };

  it("links the source from the first-run wizard", async () => {
    mockApi({ "GET /auth/setup/status": ok({ setup_needed: true }) });
    renderWithProviders(<App />);

    await screen.findByRole("heading", { name: "Welcome to Purser" });
    expectSourceLinks();
  });

  it("links the source from the sign-in page", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": fail(401, "not authenticated"),
      "GET /auth/methods": ok({ password: true, oidc: false }),
    });
    renderWithProviders(<App />);

    await screen.findByRole("heading", { name: "Sign in to Purser" });
    expectSourceLinks();
  });

  it("links the source from every signed-in page", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": ok(session(["read", "write"])),
      "GET /users": ok([]),
    });
    renderWithProviders(<App />, { route: "/users" });

    await screen.findByRole("link", { name: "Users" });
    expectSourceLinks();
  });
});
