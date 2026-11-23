import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import Accounts from "./Accounts";

const accountQueries = () =>
  apiMock.GET.mock.calls
    .filter(([path]) => path === "/accounts")
    .map(([, init]) => (init as { params: { query: Record<string, unknown> } }).params.query);

describe("Accounts page (#542)", () => {
  it("leads with the account name and a type in words, the path second (#541)", async () => {
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-cc", name: "Rewards Card", path: "liabilities.card", type: "credit_card", balance: "-380.0000" }),
      ]),
    });
    renderWithProviders(<Accounts />);

    const row = (await screen.findByRole("link", { name: "Rewards Card" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    // The first cell is the name (a link to the register), the path below it.
    expect(within(cells[0]).getByRole("link")).toHaveAttribute("href", "/accounts/a-cc");
    expect(cells[0].textContent).toBe("Rewards Cardliabilities.card");
    expect(within(row).getByText("Credit card")).toBeInTheDocument();
    expect(within(row).queryByText("credit_card")).not.toBeInTheDocument();
    expect(within(row).getByText("-$380.00")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Account", "Type", "Balance"]);
  });

  it("loads hidden and closed accounts only when asked, and badges them", async () => {
    mockApi({
      "GET /accounts": (init) => {
        const q = init.params?.query ?? {};
        return ok([
          accountOut({ id: "a-1", name: "Everyday Checking" }),
          ...(q.include_hidden ? [accountOut({ id: "a-2", name: "Old Wallet", hidden_at: "2026-10-01T00:00:00Z" })] : []),
          ...(q.include_closed ? [accountOut({ id: "a-3", name: "Closed Savings", closed_at: "2026-09-01T00:00:00Z" })] : []),
        ]);
      },
    });
    const { user } = renderWithProviders(<Accounts />);

    await screen.findByRole("link", { name: "Everyday Checking" });
    expect(accountQueries()[0]).toEqual({ include_balances: true, include_hidden: false, include_closed: false });
    expect(screen.queryByText("Old Wallet")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Show hidden"));
    const hidden = (await screen.findByRole("link", { name: "Old Wallet" })).closest("tr")!;
    expect(within(hidden).getByText("hidden")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Show closed"));
    const closed = (await screen.findByRole("link", { name: "Closed Savings" })).closest("tr")!;
    expect(within(closed).getByText("closed")).toBeInTheDocument();
    await waitFor(() =>
      expect(accountQueries()).toContainEqual({ include_balances: true, include_hidden: true, include_closed: true }),
    );
  });

  it("shows a failed load as an error and an empty ledger as such", async () => {
    mockApi({ "GET /accounts": fail(500, "database unavailable") });
    renderWithProviders(<Accounts />);
    expect(await screen.findByText("Could not load accounts: database unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/No accounts yet/)).not.toBeInTheDocument();
  });

  it("says there are no accounts yet", async () => {
    mockApi({ "GET /accounts": ok([]) });
    renderWithProviders(<Accounts />);
    expect(await screen.findByText(/No accounts yet/)).toBeInTheDocument();
  });
});

describe("Accounts page — grouped like the Dashboard (#617)", () => {
  const ACCOUNTS_KEY = "purser.accounts.collapsedGroups";
  const DASHBOARD_KEY = "purser.dashboard.collapsedGroups";
  const mixed = [
    accountOut({ id: "a-loan", name: "Mortgage", path: "liabilities.mortgage", type: "loan", balance: "-250000.0000" }),
    accountOut({ id: "a-chk", name: "Checking", path: "assets.checking", type: "checking", balance: "1200.5000" }),
    accountOut({ id: "a-brk", name: "Brokerage", path: "assets.brokerage", type: "brokerage", balance: "5000.0000" }),
    accountOut({ id: "a-sav", name: "Savings", path: "assets.savings", type: "savings", balance: "800.2500" }),
    accountOut({
      id: "a-car", name: "Family Car", path: "assets.car", type: "property", balance: "18000.0000",
      asset: { kind: "car", acquired_on: null, description: null, stale: false },
    }),
    accountOut({ id: "a-cc", name: "Card", path: "liabilities.card", type: "credit_card", balance: "-99.9900" }),
    accountOut({ id: "a-x", name: "Mystery", path: "assets.mystery", type: "crypto_wallet", balance: "1.0000" }),
  ];
  const groupNames = () =>
    screen.getAllByRole("button", { expanded: true }).concat(screen.queryAllByRole("button", { expanded: false }))
      .map((b) => b.textContent);

  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("puts each row in the Dashboard's groups, in order, with exact subtotals and the page's detail", async () => {
    mockApi({ "GET /accounts": ok(mixed) });
    renderWithProviders(<Accounts />);

    await screen.findByRole("link", { name: "Mortgage" });
    expect(groupNames()).toEqual([
      "Banking· 2",
      "Credit cards· 1",
      "Investments· 1",
      "Property· 1",
      "Loans· 1",
      "Other· 1",
    ]);
    const banking = screen.getByRole("rowgroup", { name: "Banking" });
    // 1200.50 + 800.25, added as decimals; the header row then the API's order.
    expect(within(banking).getByText("$2,000.75")).toBeInTheDocument();
    expect(within(banking).getAllByRole("link").map((l) => l.textContent)).toEqual(["Checking", "Savings"]);
    const property = screen.getByRole("rowgroup", { name: "Property" });
    const car = within(property).getByRole("link", { name: "Family Car" }).closest("tr")!;
    expect(within(car).getByText("Asset · Car")).toBeInTheDocument();
    expect(within(car).getByText("assets.car")).toBeInTheDocument();
    expect(within(screen.getByRole("rowgroup", { name: "Other" })).getByRole("link", { name: "Mystery" })).toBeInTheDocument();
    const loans = screen.getByRole("rowgroup", { name: "Loans" });
    expect(within(loans).getAllByText("-$250,000.00")).toHaveLength(2); // subtotal and row
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Account", "Type", "Balance"]);
  });

  it("says Mixed currencies rather than adding dollars to euros", async () => {
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-usd", name: "Dollar Checking", balance: "100.0000" }),
        accountOut({ id: "a-eur", name: "Euro Checking", currency: "EUR", balance: "50.0000" }),
      ]),
    });
    renderWithProviders(<Accounts />);

    const banking = await screen.findByRole("rowgroup", { name: "Banking" });
    expect(await within(banking).findByText("Mixed currencies")).toBeInTheDocument();
    expect(within(banking).getByText("€50.00")).toBeInTheDocument();
  });

  it("collapses a group to its subtotal and remembers it under its own key, not the Dashboard's", async () => {
    window.localStorage.setItem(DASHBOARD_KEY, JSON.stringify(["loans"]));
    mockApi({ "GET /accounts": ok(mixed) });
    const { user, unmount } = renderWithProviders(<Accounts />);

    // The Dashboard folded Loans; this page did not.
    expect(await screen.findByRole("link", { name: "Mortgage" })).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /^Banking/ });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Checking" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("rowgroup", { name: "Banking" })).getByText("$2,000.75")).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(ACCOUNTS_KEY)!)).toEqual(["banking"]);
    expect(JSON.parse(window.localStorage.getItem(DASHBOARD_KEY)!)).toEqual(["loans"]);

    unmount();
    renderWithProviders(<Accounts />);
    expect(await screen.findByRole("button", { name: /^Banking/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("link", { name: "Card" })).toBeInTheDocument();
  });

  it("leaves every group open when the browser blocks storage, and still folds in memory", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    mockApi({ "GET /accounts": ok(mixed) });
    const { user } = renderWithProviders(<Accounts />);

    await screen.findByRole("link", { name: "Mortgage" });
    expect(screen.queryAllByRole("button", { expanded: false })).toEqual([]);
    const toggle = screen.getByRole("button", { name: /^Banking/ });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("shows hidden and closed accounts in their own type's group, the subtotal summing the rows shown", async () => {
    mockApi({
      "GET /accounts": (init) => {
        const q = init.params?.query ?? {};
        return ok([
          accountOut({ id: "a-1", name: "Everyday Checking", balance: "100.0000" }),
          accountOut({ id: "a-cc", name: "Rewards Card", path: "liabilities.card", type: "credit_card", balance: "-20.0000" }),
          ...(q.include_hidden
            ? [accountOut({ id: "a-2", name: "Old Card", path: "liabilities.old", type: "credit_card", balance: "-5.5000", hidden_at: "2026-10-01T00:00:00Z" })]
            : []),
          ...(q.include_closed
            ? [accountOut({ id: "a-3", name: "Closed Savings", path: "assets.closed", type: "savings", balance: "0.2500", closed_at: "2026-09-01T00:00:00Z" })]
            : []),
        ]);
      },
    });
    const { user } = renderWithProviders(<Accounts />);

    await screen.findByRole("link", { name: "Everyday Checking" });
    expect(within(screen.getByRole("rowgroup", { name: "Credit cards" })).getAllByText("-$20.00")).toHaveLength(2);

    await user.click(screen.getByLabelText("Show hidden"));
    const cards = screen.getByRole("rowgroup", { name: "Credit cards" });
    const old = (await within(cards).findByRole("link", { name: "Old Card" })).closest("tr")!;
    expect(within(old).getByText("hidden")).toBeInTheDocument();
    expect(within(cards).getByText("-$25.50")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Show closed"));
    const banking = screen.getByRole("rowgroup", { name: "Banking" });
    const closed = (await within(banking).findByRole("link", { name: "Closed Savings" })).closest("tr")!;
    expect(within(closed).getByText("closed")).toBeInTheDocument();
    expect(within(banking).getByText("$100.25")).toBeInTheDocument();
  });
});
