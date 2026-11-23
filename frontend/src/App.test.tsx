import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "./App";
import { apiMock, fail, mockApi, ok } from "./test/api";
import { useLocation } from "react-router-dom";
import { accountOut, configOut, balanceHistoryOut, holdingsSummaryOut } from "./test/fixtures";
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

const ADMIN = ["read", "write", "admin"];
const MEMBER = ["read", "write"];

/** The router's current path and query, for asserting where a link lands. */
function Where() {
  const { pathname, search } = useLocation();
  return <output data-testid="where">{pathname + search}</output>;
}

function signedIn(scopes: string[], routes: Parameters<typeof mockApi>[0] = {}) {
  mockApi({
    "GET /auth/setup/status": ok({ setup_needed: false }),
    "GET /auth/me": ok(session(scopes)),
    ...routes,
  });
}

const mainNav = () => screen.getByRole("navigation", { name: "Main" });
const settingsNav = () => screen.getByRole("navigation", { name: "Settings" });

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
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
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

  it("names the sign-in tab", async () => {
    mockApi({
      "GET /auth/setup/status": ok({ setup_needed: false }),
      "GET /auth/me": fail(401, "not authenticated"),
      "GET /auth/methods": ok({ password: true, oidc: false }),
    });
    renderWithProviders(<App />);

    await screen.findByRole("heading", { name: "Sign in to Purser" });
    expect(document.title).toBe("Sign in · Purser");
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

    await screen.findByRole("link", { name: "Dashboard" });
    expectSourceLinks();
  });
});

describe("Primary navigation (#542)", () => {
  it.each([
    ["an admin", ADMIN],
    ["a member", MEMBER],
  ])("offers %s the four work areas, one Settings entry and Log out", async (_who, scopes) => {
    // /selection without ids reads nothing: the shell alone is under test.
    signedIn(scopes);
    renderWithProviders(<App />, { route: "/selection" });

    await screen.findByRole("navigation", { name: "Main" });
    const links = within(mainNav()).getAllByRole("link").map((l) => l.textContent);
    expect(links).toEqual(["Dashboard", "Accounts", "Scheduled transactions", "Import", "Settings"]);
    expect(within(mainNav()).getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  it("opens the Accounts page from the nav, active and named", async () => {
    signedIn(ADMIN, { "GET /accounts": ok([accountOut({ id: "a-1", name: "Everyday Checking" })]) });
    renderWithProviders(<App />, { route: "/accounts" });

    expect(await screen.findByRole("link", { name: "Everyday Checking" })).toHaveAttribute(
      "href",
      "/accounts/a-1",
    );
    expect(within(mainNav()).getByRole("link", { name: "Accounts" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // "/" matches exactly: Dashboard is not active on every page.
    expect(within(mainNav()).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute(
      "aria-current",
    );
    await waitFor(() => expect(document.title).toBe("Accounts · Purser"));
  });

  it("folds the links behind a Menu button on a phone, closing after a choice", async () => {
    signedIn(ADMIN, { "GET /config": ok(configOut()) });
    const { user } = renderWithProviders(<App />, { route: "/selection" });

    const menu = await within(await screen.findByRole("navigation", { name: "Main" })).findByRole(
      "button",
      { name: "Menu" },
    );
    const panel = document.getElementById(menu.getAttribute("aria-controls")!)!;
    // Hidden below md (the class), shown from md up (md:flex).
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(panel).toHaveClass("hidden", "md:flex");
    expect(panel).toContainElement(within(mainNav()).getByRole("link", { name: "Settings" }));

    await user.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(panel).not.toHaveClass("hidden");

    await user.keyboard("{Escape}");
    expect(menu).toHaveAttribute("aria-expanded", "false");

    await user.click(menu);
    await user.click(within(panel).getByRole("link", { name: "Settings" }));
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(panel).toHaveClass("hidden");
  });
});

describe("Settings area (#542)", () => {
  it("shows an admin every section, marks the active one and names the tab", async () => {
    signedIn(ADMIN, { "GET /users": ok([]) });
    renderWithProviders(<App />, { route: "/settings/users" });

    expect(await screen.findByRole("heading", { name: "Add a user" })).toBeInTheDocument();
    const sections = within(settingsNav()).getAllByRole("link").map((l) => l.textContent);
    expect(sections).toEqual([
      "General",
      "Connections",
      "Categories",
      "Rules",
      "Agent memory",
      "Prices",
      "Agents & API",
      "Security",
      "Users",
      "Audit log",
    ]);
    expect(within(settingsNav()).getByRole("link", { name: "Users" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(mainNav()).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await waitFor(() => expect(document.title).toBe("Users · Settings · Purser"));
  });

  it("hides the admin-only sections from a member and names the tab", async () => {
    signedIn(MEMBER, { "GET /categories": ok([]) });
    renderWithProviders(<App />, { route: "/settings/categories" });

    await screen.findByRole("navigation", { name: "Settings" });
    expect(await within(settingsNav()).findByRole("link", { name: "Categories" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const sections = within(settingsNav()).getAllByRole("link").map((l) => l.textContent);
    expect(sections).toEqual([
      "Connections",
      "Categories",
      "Rules",
      "Agent memory",
      "Prices",
      "Agents & API",
    ]);
    await waitFor(() => expect(document.title).toBe("Categories · Settings · Purser"));
  });

  it("tells a member who reaches an admin-only section directly, without calling it", async () => {
    signedIn(MEMBER);
    renderWithProviders(<App />, { route: "/settings/users" });

    expect(await screen.findByText(/Users is managed by an administrator/)).toBeInTheDocument();
    // GET /users is unrouted: a call would fail this test.
    expect(apiMock.GET).not.toHaveBeenCalledWith("/users");
  });

  it.each([
    [ADMIN, "/settings/general"],
    [MEMBER, "/settings/connections"],
  ])("opens the first section the session can use", async (scopes, landing) => {
    signedIn(scopes, {
      "GET /config": ok(configOut()),
      "GET /connections": ok([]),
      "GET /connections/inventory": ok([]),
    });
    renderWithProviders(
      <>
        <App />
        <Where />
      </>,
      { route: "/settings" },
    );
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(landing));
  });
});

describe("Moved routes (#542)", () => {
  // Every page an old link can land on, quiet: lists empty, config defaults.
  const quiet = {
    "GET /config": ok(configOut()),
    "GET /config/network": fail(503, "unavailable"),
    "GET /connections": ok([]),
    "GET /connections/inventory": ok([]),
    "GET /categories": ok([]),
    "GET /rules": ok([]),
    "GET /memories": ok([]),
    "GET /prices/status": fail(503, "unavailable"),
    "GET /tokens": ok([]),
    "GET /users": ok([]),
    "GET /audit/options": fail(503, "unavailable"),
    "GET /audit/activity": fail(503, "unavailable"),
    "GET /accounts": ok([]),
    "GET /networth": ok({ net_worth: "0.00", assets: "0.00", liabilities: "0.00" }),
    "GET /networth/potential": fail(503, "unavailable"),
    "GET /suggestions/counts": fail(503, "unavailable"),
    "GET /matching/unmatched": fail(503, "unavailable"),
    "GET /transactions/review-summary": fail(503, "unavailable"),
  };

  it.each([
    ["/config", "/settings/general"],
    ["/config/simplefin", "/settings/connections"],
    ["/config/categories", "/settings/categories"],
    ["/config/rules", "/settings/rules"],
    ["/config/memories", "/settings/agent-memory"],
    ["/config/prices", "/settings/prices"],
    ["/config/settings", "/settings/general"],
    ["/connections", "/settings/connections"],
    ["/tokens", "/settings/agents"],
    ["/users", "/settings/users"],
    ["/audit?view=technical&outcome=legacy", "/settings/audit?view=technical&outcome=legacy"],
    ["/review", "/"],
  ])("sends %s to %s", async (from, to) => {
    signedIn(ADMIN, quiet);
    renderWithProviders(
      <>
        <App />
        <Where />
      </>,
      { route: from },
    );
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(to));
  });

  it("keeps agent links to /selection and register links where they are", async () => {
    signedIn(MEMBER, { "GET /transactions/selection": ok([]) });
    renderWithProviders(
      <>
        <App />
        <Where />
      </>,
      { route: "/selection?id=t-1&note=two+largest" },
    );
    await waitFor(() => expect(document.title).toBe("Selected transactions · Purser"));
    expect(screen.getByTestId("where").textContent).toBe("/selection?id=t-1&note=two+largest");
  });
});
