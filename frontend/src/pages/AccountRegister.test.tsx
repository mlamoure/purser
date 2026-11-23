import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { meQuery } from "../lib/me";
import { accountOut, transferCandidate, balanceHistoryOut, holdingsSummaryOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import AccountRegister, { RegisterRow } from "./AccountRegister";

type TransactionOut = components["schemas"]["TransactionOut"];

// A card-payment instance seen from the checking register: its amount is
// the checking leg.
const instanceRow = (over: Partial<TransactionOut> = {}): TransactionOut => ({
  id: "tx-1",
  date: "2026-11-05",
  payee: null,
  description: "Card payment",
  status: "expected",
  origin: "rule",
  amount: "-340.0000",
  account_path: "assets.checking",
  category_path: null,
  transfer_account_path: "liabilities.citicard",
  is_transfer: true,
  has_linked_originals: false,
  needs_review: false,
  schedule_id: "s-1",
  schedule_pinned: false,
  split_lines: 0,
  securities_trade: false,
  securities_trade_locked: false,
  balance_adjustment: false,
  value_change: false,
  money_put_in: false,
  starting_balance: false,
  created_at: "2026-10-05T12:00:00Z",
  ...over,
});

async function openOverride() {
  mockApi({
    "GET /categories": ok([]),
    "GET /accounts": ok([]),
    "PATCH /transactions/{tx_id}": ok({}),
  });
  const view = renderWithProviders(
    <table>
      <tbody>
        <RegisterRow t={instanceRow()} accountPath="assets.checking" candidates={[]} proposed={[]} />
      </tbody>
    </table>,
  );
  await view.user.click(screen.getByRole("button", { name: "Override" }));
  return view;
}

describe("RegisterRow instance override (#350)", () => {
  it("sends the amount with the register's account, and nothing unchanged", async () => {
    const { user, container } = await openOverride();
    const [amountInput] = within(container).getAllByRole("textbox");
    await user.clear(amountInput);
    await user.type(amountInput, "-8347.00");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-1" } },
      body: { amount: "-8347.00", account: "assets.checking" },
    });
  });

  it("sends a date-only override as the date alone", async () => {
    const { user, container } = await openOverride();
    const dateInput = container.querySelector<HTMLInputElement>('input[type="date"]')!;
    await user.clear(dateInput);
    await user.type(dateInput, "2026-11-03");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-1" } },
      body: { date: "2026-11-03" },
    });
  });
});

describe("RegisterRow accessible names (#538)", () => {
  const renderRow = (t: TransactionOut) => {
    mockApi({ "GET /categories": ok([]), "GET /accounts": ok([]) });
    renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={t} accountPath="assets.checking" candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );
  };

  it("names the category select after the payee", () => {
    renderRow(instanceRow({ payee: "Acme Gym" }));
    expect(screen.getByRole("combobox", { name: "Category for Acme Gym" })).toBeInTheDocument();
  });

  it("falls back to the description when there is no payee", () => {
    renderRow(instanceRow({ payee: null }));
    expect(screen.getByRole("combobox", { name: "Category for Card payment" })).toBeInTheDocument();
  });
});

describe("RegisterRow split rows (#274, #590)", () => {
  const names = () =>
    mockApi({
      "GET /categories": ok([
        accountOut({ id: "c-1", name: "Groceries", path: "expenses.groceries", type: "category_expense", is_virtual: true }),
        accountOut({ id: "c-2", name: "Household", path: "expenses.household", type: "category_expense", is_virtual: true }),
      ]),
      "GET /accounts": ok([accountOut({ id: "a-2", name: "Reserve", path: "assets.savings" })]),
    });
  const renderRow = (t: TransactionOut, accountPath = "assets.checking") =>
    renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={t} accountPath={accountPath} candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );
  const splitRow = (over: Partial<TransactionOut> = {}) =>
    instanceRow({
      id: "tx-split",
      status: "posted",
      origin: "import",
      schedule_id: null,
      amount: "-120.0000",
      transfer_account_path: null,
      is_transfer: false,
      split_lines: 2,
      split_account_path: "assets.checking",
      ...over,
    });

  it("Split (n) opens the editor with the split's lines", async () => {
    names();
    mockApi({
      "GET /transactions/{tx_id}": ok({
        id: "tx-split",
        amount: "-120.0000",
        split: {
          account_path: "assets.checking",
          lines: [
            { target: "expenses.groceries", kind: "category", amount: "-80.0000", memo: null, backing_ids: [] },
            { target: "expenses.household", kind: "category", amount: "-40.0000", memo: "soap", backing_ids: [] },
          ],
        },
      }),
    });
    const { user } = renderRow(splitRow());
    // No category picker on the row itself.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Split (2)" }));
    expect(await screen.findByDisplayValue("soap")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Line 1 amount" })).toHaveValue("-80.00");
    expect(screen.getByRole("button", { name: "Remove split" })).toBeInTheDocument();
    expect(apiMock.GET).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-split" } },
    });
  });

  it("reads 'Split from' in the register of an account a line books to", () => {
    names();
    renderRow(splitRow({ amount: "600.0000", split_lines: 3 }), "assets.savings");
    expect(screen.getByRole("button", { name: "Split from Checking" })).toBeInTheDocument();
  });

  it("offers Split… on a posted flat row and opens the editor from its category", async () => {
    names();
    const flat = splitRow({
      id: "tx-flat",
      split_lines: 0,
      split_account_path: null,
      category_path: "expenses.groceries",
    });
    const { user } = renderRow(flat);
    await user.click(screen.getByRole("button", { name: "Split…" }));
    expect(screen.getByRole("textbox", { name: "Line 1 amount" })).toHaveValue("-120.00");
    expect(screen.getByRole("combobox", { name: "Line 1 category or account" })).toHaveValue(
      "expenses.groceries",
    );
    expect(screen.queryByRole("button", { name: "Split…" })).not.toBeInTheDocument();
  });

  it("hides Split… on rows the server would refuse", () => {
    names();
    renderRow(splitRow({ id: "a", split_lines: 0, status: "pending", origin: "import" }));
    renderRow(splitRow({ id: "b", split_lines: 0, is_transfer: true }));
    renderRow(splitRow({ id: "c", split_lines: 0, status: "expected" }));
    renderRow(splitRow({ id: "d", split_lines: 0, securities_trade: true }));
    expect(screen.queryByRole("button", { name: "Split…" })).not.toBeInTheDocument();
  });
});

describe("AccountRegister search (#141)", () => {
  it("sends one search for a burst of keystrokes, after typing pauses", async () => {
    mockApi({
      "GET /accounts/{account_id}": ok(accountOut({ id: "acct-1" })),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
    });
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/acct-1" },
    );
    const searches = () =>
      apiMock.GET.mock.calls.filter(([path]) => path === "/transactions");
    await waitFor(() => expect(searches()).toHaveLength(1)); // the initial load
    expect(searches()[0][1]?.params?.query).not.toHaveProperty("q");

    await user.type(screen.getByPlaceholderText("Search payee, description, memo…"), "starbucks");

    await waitFor(() => expect(searches()).toHaveLength(2));
    expect(searches()[1][1]?.params?.query).toMatchObject({ account: "acct-1", q: "starbucks" });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(searches()).toHaveLength(2);
  });
});

describe("AccountRegister balance chart (#594)", () => {
  it("opens a day picked on the chart in the register", async () => {
    mockApi({
      "GET /accounts/{account_id}": ok(accountOut({ id: "acct-1" })),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
    });
    renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/acct-1" },
    );

    const chart = await screen.findByRole("img", { name: /^Balance past year/ });
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    fireEvent.keyDown(chart, { key: "Enter" });

    expect(screen.getByLabelText("Start date")).toHaveValue("2026-11-02");
    expect(screen.getByLabelText("End date")).toHaveValue("2026-11-02");
    await waitFor(() =>
      expect(
        apiMock.GET.mock.calls.some(
          ([path, init]) =>
            path === "/transactions" &&
            (init as { params: { query: Record<string, unknown> } }).params.query.start === "2026-11-02",
        ),
      ).toBe(true),
    );
  });
});

describe("AccountRegister manual holdings (#390)", () => {
  const registerRoutes = (account: ReturnType<typeof accountOut>) =>
    mockApi({
      "GET /accounts/{account_id}": ok(account),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "GET /accounts/{account_id}/positions": ok([]),
      "PATCH /accounts/{account_id}": ok(account),
    });

  const render = (admin: boolean) => {
    const view = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/acct-b" },
    );
    const session: Record<string, unknown> = { scopes: admin ? ["admin"] : ["read"] };
    view.queryClient.setQueryData(meQuery.queryKey, session);
    return view;
  };

  it("shows a Positions tab only for a brokerage account tracked by hand", async () => {
    registerRoutes(
      accountOut({ id: "acct-b", type: "brokerage", path: "assets.broker", holdings_source: "manual" }),
    );
    const { user } = render(false);
    const tab = await screen.findByRole("tab", { name: "Positions" });
    await user.click(tab);
    expect(await screen.findByText(/No positions yet/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search payee, description, memo…")).not.toBeInTheDocument();
  });

  it("names the rename button and the browser tab after the account", async () => {
    registerRoutes(accountOut({ id: "acct-b", name: "Brokerage", type: "brokerage", path: "assets.broker" }));
    render(false);
    expect(await screen.findByRole("button", { name: "Rename account" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Brokerage · Purser"));
  });

  it("has no Positions tab on a feed-sourced account", async () => {
    registerRoutes(accountOut({ id: "acct-b", type: "brokerage", path: "assets.broker" }));
    render(false);
    expect(await screen.findByPlaceholderText("Search payee, description, memo…")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Positions" })).not.toBeInTheDocument();
  });

  it("offers the holdings-source switch to an admin only, behind a confirm", async () => {
    registerRoutes(accountOut({ id: "acct-b", type: "brokerage", path: "assets.broker" }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = render(true);
    await user.selectOptions(await screen.findByLabelText("Holdings source"), "manual");
    expect(confirm).toHaveBeenCalled();
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "acct-b" } },
        body: { holdings_source: "manual" },
      }),
    );
    confirm.mockRestore();
  });

  it("hides the switch from a non-admin session", async () => {
    registerRoutes(accountOut({ id: "acct-b", type: "brokerage", path: "assets.broker" }));
    render(false);
    expect(await screen.findByPlaceholderText("Search payee, description, memo…")).toBeInTheDocument();
    expect(screen.queryByLabelText("Holdings source")).not.toBeInTheDocument();
  });
});

describe("AccountRegister minimum balance (#466)", () => {
  const registerRoutes = (account: ReturnType<typeof accountOut>, patch = ok(account)) =>
    mockApi({
      "GET /accounts/{account_id}": ok(account),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /accounts/{account_id}": patch,
    });

  const render = () =>
    renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/a-1" },
    );

  it("sets a floor on an account without one, as a decimal string", async () => {
    registerRoutes(accountOut());
    const { user } = render();
    expect(await screen.findByText("No floor")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Set floor…" }));
    await user.type(screen.getByLabelText("Minimum balance"), "500.00");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "a-1" } },
        body: { min_balance: "500.00" },
      }),
    );
  });

  it("prefills the stored floor and clears it with an explicit null", async () => {
    registerRoutes(accountOut({ min_balance: "500.0000" }));
    const { user } = render();
    await user.click(await screen.findByRole("button", { name: "Edit floor" }));
    expect(screen.getByLabelText("Minimum balance")).toHaveValue("500.00");
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "a-1" } },
        body: { min_balance: null },
      }),
    );
  });

  it("keeps Save disabled for a value that is not an amount", async () => {
    registerRoutes(accountOut({ type: "credit_card", path: "liabilities.card" }));
    const { user } = render();
    await user.click(await screen.findByRole("button", { name: "Set floor…" }));
    const input = screen.getByLabelText("Minimum balance");
    await user.type(input, "five hundred");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/Enter an amount such as 500.00/)).toBeInTheDocument();
    expect(screen.getByText(/A card is negative when owed/)).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "-4500.12345");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(apiMock.PATCH).not.toHaveBeenCalled();
  });

  it("offers no floor on an account type it does not apply to", async () => {
    registerRoutes(accountOut({ type: "loan", path: "liabilities.mortgage" }));
    render();
    expect(await screen.findByPlaceholderText("Search payee, description, memo…")).toBeInTheDocument();
    expect(screen.queryByText("No floor")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /floor/ })).not.toBeInTheDocument();
  });

  it("shows the server's refusal", async () => {
    registerRoutes(accountOut(), fail(422, "min_balance is too large"));
    const { user } = render();
    await user.click(await screen.findByRole("button", { name: "Set floor…" }));
    await user.type(screen.getByLabelText("Minimum balance"), "500");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("min_balance is too large")).toBeInTheDocument();
  });
});

describe("RegisterRow transfer candidates (#386)", () => {
  // The bank row the candidate pairs: 250.00 leaving checking.
  const bankRow = () =>
    instanceRow({
      id: "t-out",
      status: "posted",
      origin: "import",
      schedule_id: null,
      amount: "-250.0000",
      description: "EXAMPLE BANK WEB DEBIT",
      transfer_account_path: null,
      is_transfer: false,
    });

  function renderRow(candidate: ReturnType<typeof transferCandidate>) {
    return renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={bankRow()} accountPath="assets.checking" candidates={[candidate]} proposed={[]} />
        </tbody>
      </table>,
    );
  }

  it("says 'not a transfer' through the pair's inbox card when it has one", async () => {
    mockApi({
      "GET /categories": ok([]),
      "GET /accounts": ok([]),
      "POST /suggestions/{suggestion_id}/decide": ok({}),
    });
    const { user } = renderRow(transferCandidate({ suggestion_id: "s-4" }));

    expect(screen.getByRole("button", { name: "Link ⇄" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not a transfer" }));

    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-4" } },
      body: { decision: "reject", reject_scope: "exact" },
    });
    expect(apiMock.POST).toHaveBeenCalledTimes(1);
  });

  it("says 'not a transfer' through the candidate endpoint when the pair has no card", async () => {
    mockApi({
      "GET /categories": ok([]),
      "GET /accounts": ok([]),
      "POST /matching/transfer-candidates/reject": ok({
        suggestion_id: "s-9",
        status: "rejected",
        already_rejected: false,
      }),
    });
    const { user } = renderRow(transferCandidate());

    await user.click(screen.getByRole("button", { name: "Not a transfer" }));

    expect(apiMock.POST).toHaveBeenCalledWith("/matching/transfer-candidates/reject", {
      body: { outgoing_id: "t-out", incoming_id: "t-in" },
    });
    expect(apiMock.POST).toHaveBeenCalledTimes(1);
  });
});

describe("AccountRegister securities trades (#390 phase 1b)", () => {
  it("shows a trade's cash as a read-only Securities trade, with no category picker", async () => {
    mockApi({ "GET /categories": ok([]), "GET /accounts": ok([]) });
    const tradeRow = instanceRow({
      id: "tx-trade",
      status: "posted",
      origin: "system",
      schedule_id: null,
      amount: "995.0000",
      category_path: "equity.securities",
      transfer_account_path: null,
      is_transfer: false,
      securities_trade: true,
      securities_trade_locked: true,
    });
    renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={tradeRow} accountPath="assets.plan" candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );
    expect(screen.getByText("Securities trade")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("shows an asset's value change as a read-only Value change", async () => {
    mockApi({ "GET /categories": ok([]), "GET /accounts": ok([]) });
    const change = instanceRow({
      id: "tx-change",
      status: "posted",
      origin: "human",
      schedule_id: null,
      amount: "25000.0000",
      category_path: "equity.revaluation",
      transfer_account_path: null,
      is_transfer: false,
      value_change: true,
    });
    renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={change} accountPath="assets.house" candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );
    expect(screen.getByText("Value change")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("undoes a value change through its reversal, and labels money put in", async () => {
    mockApi({
      "GET /categories": ok([]),
      "GET /accounts": ok([]),
      "POST /value-changes/{transaction_id}/reverse": ok({ reversed: true }),
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const putIn = instanceRow({
      id: "tx-put-in",
      status: "posted",
      origin: "human",
      schedule_id: null,
      amount: "450000.0000",
      category_path: "equity.capital",
      transfer_account_path: null,
      is_transfer: false,
      value_change: true,
      money_put_in: true,
    });
    const { user } = renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={putIn} accountPath="assets.house" candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );
    expect(screen.getByText("Money put in")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Undo the .* money put in/ }));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/value-changes/{transaction_id}/reverse", {
        params: { path: { transaction_id: "tx-put-in" } },
      }),
    );
    confirm.mockRestore();
  });

  it("never offers a trade's cash as the row that paid a bill", async () => {
    const bankRow = instanceRow({
      id: "tx-bank",
      status: "posted",
      origin: "import",
      schedule_id: null,
      description: "ACH DEPOSIT",
      amount: "100.0000",
      transfer_account_path: null,
      is_transfer: false,
    });
    const tradeCash = { ...bankRow, id: "tx-cash", description: "Sale of 1 XYZ", securities_trade: true };
    mockApi({
      "GET /categories": ok([]),
      "GET /accounts": ok([]),
      "GET /transactions": ok({ items: [bankRow, tradeCash], next_cursor: null }),
    });
    const view = renderWithProviders(
      <table>
        <tbody>
          <RegisterRow
            t={instanceRow({ amount: "100.0000", transfer_account_path: null, is_transfer: false })}
            accountPath="assets.plan"
            candidates={[]}
            proposed={[]}
          />
        </tbody>
      </table>,
    );
    await view.user.click(screen.getByRole("button", { name: "Mark paid" }));
    expect(await screen.findByText("ACH DEPOSIT")).toBeInTheDocument();
    expect(screen.queryByText("Sale of 1 XYZ")).not.toBeInTheDocument();
  });

  it("lets an admin say a hand-tracked account's feed balance is its cash", async () => {
    const account = accountOut({
      id: "acct-b",
      type: "brokerage",
      path: "assets.broker",
      holdings_source: "manual",
    });
    mockApi({
      "GET /accounts/{account_id}": ok(account),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [], total: 0, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /accounts/{account_id}": ok({ ...account, feed_balance_is_cash: true }),
    });
    const view = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/acct-b" },
    );
    const session: Record<string, unknown> = { scopes: ["admin"] };
    view.queryClient.setQueryData(meQuery.queryKey, session);
    await view.user.click(await screen.findByLabelText("Feed balance is cash"));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "acct-b" } },
        body: { feed_balance_is_cash: true },
      }),
    );
  });
});


describe("Account register guided review", () => {
  it("honors attention links and confirms only the selected posted rows", async () => {
    mockApi({
      "GET /categories": ok([]),
      "GET /accounts": ok([]),
      "GET /accounts/{account_id}": ok(accountOut()),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": ok({ items: [
        instanceRow({ id: "posted", status: "posted", origin: "import", needs_review: true, schedule_id: null, payee: "Example shop" }),
        instanceRow({ id: "pending", status: "pending", origin: "import", needs_review: true, schedule_id: null, payee: "Pending shop" }),
      ], total: 2, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "GET /auth/me": ok({ scopes: ["read", "write", "admin"] }),
      "POST /transactions/mark-reviewed": ok({ reviewed: 1 }),
    });
    const { user } = renderWithProviders(<Routes><Route path="/accounts/:accountId" element={<AccountRegister />} /></Routes>, { route: "/accounts/a1?reviewed=false&status=posted" });
    await screen.findByText("Example shop");
    expect(apiMock.GET).toHaveBeenCalledWith("/transactions", expect.objectContaining({ params: { query: expect.objectContaining({ account: "a1", reviewed: false, status: ["posted"] }) } }));
    expect(screen.queryByLabelText(/Select Pending shop/)).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Select all posted transactions awaiting review on this page"));
    await user.click(screen.getByRole("button", { name: "Review selected (1)" }));
    const preview = screen.getByRole("region", { name: "Confirm transaction review" });
    expect(within(preview).getByText(/Example shop/)).toBeInTheDocument();
    expect(within(preview).queryByText(/Pending shop/)).not.toBeInTheDocument();
    expect(apiMock.POST).not.toHaveBeenCalled();
    await user.click(within(preview).getByRole("button", { name: "Confirm review" }));
    await waitFor(() => expect(apiMock.POST).toHaveBeenCalledWith("/transactions/mark-reviewed", { body: { transaction_ids: ["posted"], escalate: false } }));
  });
});

describe("Account register: correcting a posted row (#541)", () => {
  const CATEGORIES = [
    accountOut({ id: "c-1", name: "Groceries", path: "expenses.groceries", type: "category_expense", is_virtual: true }),
    accountOut({ id: "c-2", name: "Dining out", path: "expenses.dining", type: "category_expense", is_virtual: true }),
  ];
  const posted = (over: Partial<TransactionOut> = {}) =>
    instanceRow({
      id: "tx-orig",
      status: "posted",
      origin: "import",
      schedule_id: null,
      payee: "Example Market",
      amount: "-42.1000",
      category_path: "expenses.groceries",
      transfer_account_path: null,
      is_transfer: false,
      ...over,
    });

  it("recategorizes without a confirm, then offers Undo on the corrected row", async () => {
    // The register lists the original until the correction lands, then its
    // replacement (a new id); after the undo, a second replacement.
    let rows = [posted()];
    const confirm = vi.spyOn(window, "confirm");
    mockApi({
      "GET /categories": ok(CATEGORIES),
      "GET /accounts": ok([]),
      "GET /accounts/{account_id}": ok(accountOut()),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": () => ok({ items: rows, total: rows.length, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /transactions/{tx_id}": (init) => {
        const id = init.params?.path?.tx_id;
        const category = (init.body as { category: string }).category;
        const next = posted({ id: id === "tx-orig" ? "tx-fix" : "tx-undo", category_path: category });
        rows = [next];
        return ok(next);
      },
    });
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/a-1" },
    );

    const picker = await screen.findByRole("combobox", { name: "Category for Example Market" });
    await within(picker).findByRole("option", { name: "Dining out" });
    await user.selectOptions(picker, "Dining out");

    expect(confirm).not.toHaveBeenCalled();
    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-orig" } },
      body: { category: "expenses.dining", account: "assets.checking" },
    });
    const undo = await screen.findByRole("button", { name: "Undo: back to Groceries" });
    expect(screen.getByText("Corrected")).toBeInTheDocument();

    await user.click(undo);
    expect(apiMock.PATCH).toHaveBeenLastCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-fix" } },
      body: { category: "expenses.groceries", account: "assets.checking" },
    });
    await waitFor(() => expect(screen.queryByText("Corrected")).not.toBeInTheDocument());
    expect(
      await screen.findByRole("combobox", { name: "Category for Example Market" }),
    ).toHaveValue("expenses.groceries");
    expect(apiMock.PATCH).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it("offers no Undo after recategorizing a pending row, which is edited in place", async () => {
    let rows = [posted({ id: "tx-p", status: "pending" })];
    mockApi({
      "GET /categories": ok(CATEGORIES),
      "GET /accounts": ok([]),
      "GET /accounts/{account_id}": ok(accountOut()),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": () => ok({ items: rows, total: rows.length, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /transactions/{tx_id}": (init) => {
        rows = [posted({ id: "tx-p", status: "pending", category_path: (init.body as { category: string }).category })];
        return ok(rows[0]);
      },
    });
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/a-1" },
    );
    const picker = await screen.findByRole("combobox", { name: "Category for Example Market" });
    await within(picker).findByRole("option", { name: "Dining out" });
    await user.selectOptions(picker, "Dining out");
    await waitFor(() => expect(picker).toHaveValue("expenses.dining"));
    expect(screen.queryByRole("button", { name: /Undo/ })).not.toBeInTheDocument();
  });

  it("shows the server's refusal of an undo on the row", async () => {
    let rows = [posted()];
    mockApi({
      "GET /categories": ok(CATEGORIES),
      "GET /accounts": ok([]),
      "GET /accounts/{account_id}": ok(accountOut()),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": () => ok({ items: rows, total: rows.length, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /transactions/{tx_id}": (init) => {
        if (init.params?.path?.tx_id === "tx-fix") return fail(422, "transaction is already corrected");
        rows = [posted({ id: "tx-fix", category_path: "expenses.dining" })];
        return ok(rows[0]);
      },
    });
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: "/accounts/a-1" },
    );
    const picker = await screen.findByRole("combobox", { name: "Category for Example Market" });
    await within(picker).findByRole("option", { name: "Dining out" });
    await user.selectOptions(picker, "Dining out");
    await user.click(await screen.findByRole("button", { name: "Undo: back to Groceries" }));
    expect(await screen.findByText("transaction is already corrected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo: back to Groceries" })).toBeInTheDocument();
  });
});

describe("Account register: securities trades on a brokerage account (#591)", () => {
  const CATEGORIES = [
    accountOut({ id: "c-1", name: "Groceries", path: "expenses.groceries", type: "category_expense", is_virtual: true }),
  ];
  const broker = accountOut({ id: "acct-b", type: "brokerage", path: "assets.broker", name: "Broker" });
  const buyRow = (over: Partial<TransactionOut> = {}) =>
    instanceRow({
      id: "tx-buy",
      status: "posted",
      origin: "import",
      schedule_id: null,
      payee: "Example Buy",
      description: "Bought shares of an example fund",
      amount: "-250.0000",
      account_path: "assets.broker",
      category_path: "expenses.uncategorized",
      transfer_account_path: null,
      is_transfer: false,
      ...over,
    });

  function renderRegister(rows: TransactionOut[], account = broker) {
    let current = rows;
    mockApi({
      "GET /categories": ok(CATEGORIES),
      "GET /accounts": ok([]),
      "GET /accounts/{account_id}": ok(account),
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()),
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()),
      "GET /transactions": () => ok({ items: current, total: current.length, limit: 50, offset: 0 }),
      "GET /matching/transfer-candidates": ok([]),
      "GET /matching/proposed": ok([]),
      "PATCH /transactions/{tx_id}": () => {
        // A pending row is marked in place: same id.
        current = [
          buyRow({ status: "pending", category_path: "equity.securities", securities_trade: true }),
        ];
        return ok({ ...current[0], suggestion_id: "sg-1" });
      },
      "PATCH /accounts/{account_id}": ok({ ...account, balance_true_up: false }),
    });
    return renderWithProviders(
      <Routes>
        <Route path="/accounts/:accountId" element={<AccountRegister />} />
      </Routes>,
      { route: `/accounts/${account.id}` },
    );
  }

  it("offers Securities trade on a feed-holdings brokerage register and sends securities_trade", async () => {
    const { user } = renderRegister([buyRow({ status: "pending" })]);
    const picker = await screen.findByRole("combobox", { name: "Category for Example Buy" });
    const option = await within(picker).findByRole("option", { name: "Securities trade" });
    await user.selectOptions(picker, option);
    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-buy" } },
      body: { securities_trade: true },
    });
    expect(await screen.findByText(/A rule for rows like this one is waiting/)).toBeInTheDocument();
    // The marked row reads "Securities trade" and can be changed back.
    expect(
      await screen.findByRole("combobox", { name: "Category for Example Buy" }),
    ).toHaveValue(":securities_trade");
  });

  it("never offers it on a checking register", async () => {
    renderRegister([buyRow({ account_path: "assets.checking" })], accountOut({ id: "a-1" }));
    const picker = await screen.findByRole("combobox", { name: "Category for Example Buy" });
    await within(picker).findByRole("option", { name: "Groceries" });
    expect(within(picker).queryByRole("option", { name: "Securities trade" })).not.toBeInTheDocument();
  });

  it("labels a recorded trade's cash and a balance adjustment, with no picker", async () => {
    renderRegister([
      buyRow({ id: "tx-cash", payee: "Trade", securities_trade: true, securities_trade_locked: true }),
      buyRow({ id: "tx-adj", payee: "Balance adjustment", securities_trade: true, balance_adjustment: true }),
    ]);
    expect(await screen.findByText("Balance adjustment (invested cash)")).toBeInTheDocument();
    expect(screen.getByText("Securities trade")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Category for Trade" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Category for Balance adjustment" })).not.toBeInTheDocument();
  });

  it("labels the starting balance from the bank, with no picker", async () => {
    renderRegister(
      [buyRow({ id: "tx-start", payee: "Starting balance", account_path: "assets.checking", starting_balance: true })],
      accountOut({ id: "a-1" }),
    );
    expect(await screen.findByText("Starting balance", { selector: "span" })).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Category for Starting balance" }),
    ).not.toBeInTheDocument();
  });

  it("switches the balance true-up off from the account settings", async () => {
    const { user } = renderRegister([], { ...broker, balance_true_up: true });
    const toggle = await screen.findByLabelText("Keep cash in step with the broker's balance");
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "acct-b" } },
        body: { balance_true_up: false },
      }),
    );
  });

  it("shows no true-up switch on a hand-tracked brokerage account", async () => {
    renderRegister([], { ...broker, holdings_source: "manual" });
    await screen.findByText("No transactions match.");
    expect(screen.queryByLabelText("Keep cash in step with the broker's balance")).not.toBeInTheDocument();
  });
});
