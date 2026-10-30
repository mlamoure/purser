import { screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { meQuery } from "../lib/me";
import { accountOut } from "../test/fixtures";
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

describe("RegisterRow split rows (#274)", () => {
  it("shows a read-only Split (n) that lists the category legs", async () => {
    mockApi({
      "GET /categories": ok([]),
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

    expect(await screen.findByText("expenses.groceries")).toBeInTheDocument();
    expect(screen.getByText("expenses.household")).toBeInTheDocument();
    expect(screen.queryByText("assets.checking")).not.toBeInTheDocument();
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
