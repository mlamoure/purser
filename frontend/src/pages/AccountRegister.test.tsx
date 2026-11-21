import { screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { meQuery } from "../lib/me";
import { accountOut, transferCandidate } from "../test/fixtures";
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
  value_change: false,
  money_put_in: false,
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

describe("RegisterRow split rows (#274)", () => {
  it("shows a read-only Split (n) that lists the category legs", async () => {
    mockApi({
      "GET /categories": ok([
        accountOut({ id: "c-1", name: "Groceries", path: "expenses.groceries", type: "category_expense", is_virtual: true }),
      ]),
      "GET /accounts": ok([]),
      "GET /transactions/{tx_id}": ok({
        postings: [
          { id: "p-1", account_id: "a-1", account_path: "assets.checking", amount: "-120.0000", currency: "USD", memo: null },
          { id: "p-2", account_id: "c-1", account_path: "expenses.groceries", amount: "80.0000", currency: "USD", memo: null },
          { id: "p-3", account_id: "c-2", account_path: "expenses.household", amount: "40.0000", currency: "USD", memo: "soap" },
        ],
      }),
    });
    const split = instanceRow({
      id: "tx-split",
      status: "posted",
      origin: "import",
      schedule_id: null,
      amount: "-120.0000",
      transfer_account_path: null,
      is_transfer: false,
      split_lines: 2,
    });
    const { user } = renderWithProviders(
      <table>
        <tbody>
          <RegisterRow t={split} accountPath="assets.checking" candidates={[]} proposed={[]} />
        </tbody>
      </table>,
    );

    // No category picker: split rows are read-only.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Split (2)" }));

    // Legs read by name (#541); a path no list knows reads as its last label.
    expect(await screen.findByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("Household")).toBeInTheDocument();
    expect(screen.queryByText("expenses.groceries")).not.toBeInTheDocument();
    expect(screen.queryByText("Checking")).not.toBeInTheDocument();
    expect(apiMock.GET).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-split" } },
    });
  });
});

describe("AccountRegister search (#141)", () => {
  it("sends one search for a burst of keystrokes, after typing pauses", async () => {
    mockApi({
      "GET /accounts/{account_id}": ok(accountOut({ id: "acct-1" })),
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

    await user.type(screen.getByPlaceholderText("Search description…"), "starbucks");

    await waitFor(() => expect(searches()).toHaveLength(2));
    expect(searches()[1][1]?.params?.query).toMatchObject({ account: "acct-1", q: "starbucks" });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(searches()).toHaveLength(2);
  });
});

describe("AccountRegister manual holdings (#390)", () => {
  const registerRoutes = (account: ReturnType<typeof accountOut>) =>
    mockApi({
      "GET /accounts/{account_id}": ok(account),
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
    expect(screen.queryByPlaceholderText("Search description…")).not.toBeInTheDocument();
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
    expect(await screen.findByPlaceholderText("Search description…")).toBeInTheDocument();
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
    expect(await screen.findByPlaceholderText("Search description…")).toBeInTheDocument();
    expect(screen.queryByLabelText("Holdings source")).not.toBeInTheDocument();
  });
});

describe("AccountRegister minimum balance (#466)", () => {
  const registerRoutes = (account: ReturnType<typeof accountOut>, patch = ok(account)) =>
    mockApi({
      "GET /accounts/{account_id}": ok(account),
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
    expect(await screen.findByPlaceholderText("Search description…")).toBeInTheDocument();
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
