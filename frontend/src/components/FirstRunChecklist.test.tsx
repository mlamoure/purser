import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok, type Route } from "../test/api";
import { renderWithProviders } from "../test/render";
import FirstRunChecklist, { NewBankAccounts } from "./FirstRunChecklist";

const config = (acknowledgedAt: string | null) => ({
  encryption: {
    field_encryption_active: true,
    canary_ok: true,
    key_fingerprint: "abcd1234",
    key_acknowledged_at: acknowledgedAt,
    encrypted_scopes: [],
  },
});
const connection = {
  id: "c-1",
  provider: "simplefin",
  status: "active",
  last_fetch_at: "2026-11-17T12:00:00Z",
};
const feedAccount = (mapped: boolean) => ({
  external_id: mapped ? "ext-mapped" : "ext-new",
  name: mapped ? "Everyday Checking" : "New Savings",
  org: "Example Bank",
  currency: "USD",
  ignored: false,
  mapped_account_id: mapped ? "a-1" : null,
  mapped_account_path: mapped ? "assets.everyday_checking" : null,
});
const apiToken = { id: "t-1", name: "agent", kind: "api", scopes: ["read", "write"] };
const session = { id: "t-2", name: "session", kind: "session", scopes: ["read", "write", "admin"] };

/** Server state for the four items; anything not given is "all done". */
function server(over: Record<string, Route> = {}) {
  mockApi({
    "GET /config": ok(config("2026-11-17T12:00:00Z")),
    "GET /connections": ok([connection]),
    "GET /connections/{connection_id}/discovered": ok([feedAccount(true)]),
    "GET /tokens": ok([session, apiToken]),
    ...over,
  });
}

const item = (name: RegExp) => screen.getByText(name).closest("li")!;

afterEach(() => {
  window.localStorage.clear();
});

describe("FirstRunChecklist (#539)", () => {
  it("derives each item from server state, says why and links the undone ones", async () => {
    server({
      "GET /config": ok(config(null)),
      "GET /connections/{connection_id}/discovered": ok([feedAccount(false)]),
      "GET /tokens": ok([session]),
    });
    renderWithProviders(<FirstRunChecklist />);

    const card = await screen.findByRole("region", { name: "Finish setting up" });
    expect(within(card).getAllByRole("listitem")).toHaveLength(4);

    const key = item(/Record your encryption key/);
    expect(within(key).getByText("to do")).toBeInTheDocument();
    expect(within(key).getByText(/Nobody has confirmed saving/)).toBeInTheDocument();
    expect(within(key).getByRole("link", { name: "Show and record the key" }))
      .toHaveAttribute("href", "/settings/security");
    expect(within(item(/Connect a SimpleFIN bank feed/)).getByText("done")).toBeInTheDocument();
    expect(within(item(/Connect a SimpleFIN bank feed/)).queryByRole("link")).not.toBeInTheDocument();
    // The open accounts are named, and the link opens the first one's form.
    const map = item(/Map your bank accounts/);
    expect(within(map).getByText(/Not in Purser yet: New Savings \(Example Bank\)/)).toBeInTheDocument();
    expect(within(map).getByRole("link", { name: "Map accounts" }))
      .toHaveAttribute("href", "/settings/connections?map=ext-new");
    // A session is not an agent: only an API token counts.
    expect(within(item(/Connect your AI agent/)).getByRole("link", { name: "Create a token" }))
      .toHaveAttribute("href", "/settings/agents");
  });

  it("leaves a bank account delivered after setup to the Dashboard notice (#560)", async () => {
    server({
      "GET /config": ok(config(null)),
      "GET /connections/{connection_id}/discovered": ok([feedAccount(true), feedAccount(false)]),
    });
    renderWithProviders(<FirstRunChecklist />);

    await screen.findByRole("region", { name: "Finish setting up" });
    expect(screen.queryByText(/Map your bank accounts/)).not.toBeInTheDocument();
  });

  it("is done with mapping once the only unmapped account is ignored (#560)", async () => {
    server({
      "GET /connections/{connection_id}/discovered": ok([
        feedAccount(true),
        { ...feedAccount(false), ignored: true },
      ]),
    });
    renderWithProviders(<FirstRunChecklist />);

    await waitFor(() =>
      expect(apiMock.GET).toHaveBeenCalledWith(
        "/connections/{connection_id}/discovered",
        expect.anything(),
      ),
    );
    await waitFor(() => expect(apiMock.GET).toHaveBeenCalledWith("/tokens"));
    expect(screen.queryByRole("region", { name: "Finish setting up" })).not.toBeInTheDocument();
  });

  it("asks for a feed and leaves mapping out while none is connected", async () => {
    server({ "GET /connections": ok([]) });
    renderWithProviders(<FirstRunChecklist />);

    expect(await screen.findByRole("link", { name: "Connect a feed" })).toBeInTheDocument();
    expect(screen.queryByText(/Map /)).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith(
      "/connections/{connection_id}/discovered",
      expect.anything(),
    );
  });

  it("is hidden when everything is done", async () => {
    server();
    renderWithProviders(<FirstRunChecklist />);

    await waitFor(() => expect(apiMock.GET).toHaveBeenCalledWith("/tokens"));
    await waitFor(() =>
      expect(apiMock.GET).toHaveBeenCalledWith(
        "/connections/{connection_id}/discovered",
        expect.anything(),
      ),
    );
    expect(screen.queryByRole("region", { name: "Finish setting up" })).not.toBeInTheDocument();
  });

  it("leaves out an item whose read failed instead of guessing", async () => {
    server({
      "GET /tokens": fail(500, "boom"),
      "GET /config": ok(config(null)),
    });
    renderWithProviders(<FirstRunChecklist />);

    await screen.findByRole("region", { name: "Finish setting up" });
    expect(screen.queryByText(/Connect your AI agent/)).not.toBeInTheDocument();
    expect(screen.getByText(/Record your encryption key/)).toBeInTheDocument();
  });

  it("stays dismissed in this browser", async () => {
    server({ "GET /config": ok(config(null)) });
    const first = renderWithProviders(<FirstRunChecklist />);
    await first.user.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region", { name: "Finish setting up" })).not.toBeInTheDocument();
    first.unmount();

    apiMock.GET.mockClear();
    renderWithProviders(<FirstRunChecklist />);
    expect(screen.queryByRole("region", { name: "Finish setting up" })).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalled(); // dismissed: no reads at all
  });
});

describe("NewBankAccounts (#560)", () => {
  it("names each account waiting after setup and links to its row", async () => {
    server({
      "GET /connections/{connection_id}/discovered": ok([feedAccount(true), feedAccount(false)]),
    });
    renderWithProviders(<NewBankAccounts />);

    const card = await screen.findByRole("region", { name: "New bank accounts" });
    expect(within(card).getByText("1 bank account not in Purser yet")).toBeInTheDocument();
    expect(within(card).getByText("New Savings")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Map or ignore" }))
      .toHaveAttribute("href", "/settings/connections?map=ext-new");
  });

  it("shows nothing for an ignored account, during first-run mapping, or on a failed read", async () => {
    const discovered = "/connections/{connection_id}/discovered";
    const cases: Route[] = [
      ok([feedAccount(true), { ...feedAccount(false), ignored: true }]),
      ok([feedAccount(false)]), // nothing mapped yet: the checklist has it
      fail(500, "boom"),
    ];
    for (const route of cases) {
      apiMock.GET.mockClear();
      server({ [`GET ${discovered}`]: route });
      const view = renderWithProviders(<NewBankAccounts />);
      await waitFor(() =>
        expect(apiMock.GET).toHaveBeenCalledWith(discovered, expect.anything()),
      );
      await new Promise((r) => setTimeout(r, 20));
      expect(screen.queryByRole("region", { name: "New bank accounts" })).not.toBeInTheDocument();
      view.unmount();
    }
  });
});
