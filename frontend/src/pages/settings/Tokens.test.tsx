import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { meQuery } from "../../lib/me";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import Tokens from "./Tokens";

type TokenRow = {
  id: string;
  name: string;
  kind: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  is_current: boolean;
};

const token = (over: Partial<TokenRow> = {}): TokenRow => ({
  id: "t-1",
  name: "ci-agent",
  kind: "api",
  scopes: ["read", "write"],
  expires_at: null,
  last_used_at: null,
  created_at: "2026-01-05T12:00:00Z",
  is_current: false,
  ...over,
});

const currentSession = token({
  id: "s-1",
  name: "session:password:admin@example.test",
  kind: "session",
  scopes: ["read", "write", "admin"],
  is_current: true,
});

/** Render the page for an admin (or member) session: the create form is
 * admin-only, read from the "me" query App loads first. */
function renderTokens(admin = true) {
  const view = renderWithProviders(<Tokens />);
  const session: Record<string, unknown> = {
    scopes: admin ? ["read", "write", "admin"] : ["read", "write"],
  };
  view.queryClient.setQueryData(meQuery.queryKey, session);
  return view;
}

afterEach(() => vi.restoreAllMocks());

describe("Tokens: create", () => {
  it("mints a read + write token by default and shows the secret once", async () => {
    mockApi({
      "GET /tokens": ok([]),
      "POST /tokens": ({ body }) =>
        ok(
          {
            ...(body as { name: string; scopes: string[] }),
            id: "t-1",
            kind: "api",
            expires_at: null,
            last_used_at: null,
            created_at: "2026-01-05T12:00:00Z",
            is_current: false,
            token: "synthetic-token-value",
          },
          201,
        ),
    });
    const { user } = renderTokens();
    expect(await screen.findByText("No API tokens yet.")).toBeInTheDocument();

    await user.type(await screen.findByLabelText("Token name"), "ci-agent");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByText("synthetic-token-value")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/tokens", {
      body: { name: "ci-agent", scopes: ["read", "write"], expires_at: undefined },
    });
    expect(screen.getByLabelText("Token name")).toHaveValue("");

    // The once-shown secret has a copy button.
    await user.click(screen.getByRole("button", { name: "Copy token" }));
    expect(await navigator.clipboard.readText()).toBe("synthetic-token-value");
  });

  it("shows the full MCP URL, ready to copy", async () => {
    mockApi({ "GET /tokens": ok([]) });
    const { user } = renderTokens();
    const url = `${window.location.origin}/mcp`;

    // Wait for the admin view: the session is seeded just after the first render.
    const copy = await screen.findByRole("button", { name: "Copy MCP URL" });
    expect(screen.getByText(url)).toBeInTheDocument();
    await user.click(copy);
    expect(await navigator.clipboard.readText()).toBe(url);
  });

  it("says why Create token is disabled", async () => {
    mockApi({ "GET /tokens": ok([]) });
    const { user } = renderTokens();
    const create = await screen.findByRole("button", { name: "Create token" });

    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription("Enter a token name.");
    await user.type(screen.getByLabelText("Token name"), "ci-agent");
    expect(create).toBeEnabled();
    expect(screen.queryByText("Enter a token name.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /^read/ }));
    await user.click(screen.getByRole("checkbox", { name: /^write/ }));
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription("Choose at least one scope.");
  });

  it("offers a member session no create form (POST /tokens is admin-only)", async () => {
    mockApi({ "GET /tokens": ok([currentSession]) });
    renderTokens(false);

    expect(await screen.findByText(/Only an admin can create API tokens/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    // A member still manages their own sessions here.
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});

describe("Tokens: revoke", () => {
  it("names the token in a confirm and does nothing when declined", async () => {
    mockApi({ "GET /tokens": ok([token()]) });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderTokens();

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('"ci-agent"'));
    expect(apiMock.DELETE).not.toHaveBeenCalled();
  });

  it("revokes a confirmed token and refreshes the list", async () => {
    let rows = [token()];
    mockApi({
      "GET /tokens": () => ok(rows),
      "DELETE /tokens/{token_id}": () => {
        rows = [];
        return ok({ status: "revoked" });
      },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderTokens();

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(apiMock.DELETE).toHaveBeenCalledWith("/tokens/{token_id}", {
      params: { path: { token_id: "t-1" } },
    });
    expect(await screen.findByText("No API tokens yet.")).toBeInTheDocument();
  });

  it("shows a failed revoke instead of failing silently", async () => {
    mockApi({
      "GET /tokens": ok([token()]),
      "DELETE /tokens/{token_id}": fail(404, "token not found"),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderTokens();

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not revoke: token not found");
    expect(screen.getByText("ci-agent")).toBeInTheDocument();
  });

  it("warns that revoking the current session signs you out", async () => {
    mockApi({ "GET /tokens": ok([currentSession]) });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderTokens();

    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("signs this browser out"));
    await waitFor(() => expect(apiMock.POST).not.toHaveBeenCalled());
  });
});

describe("Tokens: load state and times (#540)", () => {
  it("shows a failed list load as an error in both lists, not as none", async () => {
    mockApi({ "GET /tokens": fail(500, "database unavailable") });
    renderTokens();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.map((a) => a.textContent)).toEqual([
      "Could not load API tokens: database unavailable",
      "Could not load login sessions: database unavailable",
    ]);
    expect(screen.queryByText("No API tokens yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("No active sessions.")).not.toBeInTheDocument();
  });

  it("shows created and last-used times in the household time zone", async () => {
    mockApi({
      "GET /clock": ok({ ledger_today: "2026-01-05", timezone: "America/New_York" }),
      "GET /tokens": ok([token({ last_used_at: "2026-01-06T01:30:00Z" })]),
    });
    renderTokens();

    expect(await screen.findByText("Jan 5, 2026, 7:00 AM")).toBeInTheDocument();
    // 01:30 UTC on the 6th is still the evening of the 5th in New York.
    expect(screen.getByText("Jan 5, 2026, 8:30 PM")).toBeInTheDocument();
  });
});
