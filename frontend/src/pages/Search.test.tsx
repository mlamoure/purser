import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import Search from "./Search";

type TransactionOut = components["schemas"]["TransactionOut"];

const row = (over: Partial<TransactionOut> = {}): TransactionOut => ({
  id: "tx-1",
  date: "2026-03-02",
  payee: "Blue Cup",
  description: "POS PURCHASE BLUE CUP",
  status: "posted",
  origin: "import",
  amount: "-4.5000",
  account_path: "assets.checking",
  category_path: "expenses.coffee",
  transfer_account_path: null,
  is_transfer: false,
  has_linked_originals: false,
  needs_review: false,
  schedule_id: null,
  schedule_pinned: false,
  split_lines: 0,
  securities_trade: false,
  securities_trade_locked: false,
  balance_adjustment: false,
  value_change: false,
  money_put_in: false,
  starting_balance: false,
  created_at: "2026-03-02T12:00:00Z",
  ...over,
});

const page = (items: TransactionOut[], extra: Record<string, unknown> = {}) =>
  ok({ items, total: items.length, limit: 50, offset: 0, ...extra });

function lists() {
  mockApi({
    "GET /accounts": ok([accountOut({ id: "a-1", name: "Everyday Checking", path: "assets.checking" })]),
    "GET /categories": ok([accountOut({ id: "c-1", name: "Coffee", path: "expenses.coffee", type: "category_expense" })]),
  });
}

describe("Search across every account (#141)", () => {
  it("searches as you type, with no account, and links each row to its register", async () => {
    lists();
    mockApi({ "GET /transactions": page([row()]) });
    const { user } = renderWithProviders(<Search />, { route: "/search" });
    expect(apiMock.GET).not.toHaveBeenCalledWith("/transactions", expect.anything());

    await user.type(screen.getByLabelText("Search all accounts"), "blue cup");
    const link = await screen.findByRole("link", { name: "Everyday Checking" });
    expect(link).toHaveAttribute("href", "/accounts/a-1");
    const table = screen.getByRole("table");
    expect(within(table).getByText("POS PURCHASE BLUE CUP")).toBeInTheDocument();
    expect(within(table).getByText("Coffee")).toBeInTheDocument();
    const calls = apiMock.GET.mock.calls.filter(([path]) => path === "/transactions");
    expect(calls.at(-1)?.[1]).toEqual({ params: { query: { q: "blue cup", limit: 50, offset: 0 } } });
  });

  it("reads the query from the address and offers did_you_mean", async () => {
    lists();
    mockApi({
      "GET /transactions": (init) =>
        init.params?.query?.q === "starbuks"
          ? page([], { did_you_mean: "starbucks" })
          : page([row({ id: "tx-2", description: "STARBUCKS #12" })]),
    });
    const { user } = renderWithProviders(<Search />, { route: "/search?q=starbuks" });
    expect(await screen.findByText(/No transactions match/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "starbucks" }));
    expect(await screen.findByText("STARBUCKS #12")).toBeInTheDocument();
    expect(screen.getByLabelText("Search all accounts")).toHaveValue("starbucks");
  });

  it("shows a malformed query's message", async () => {
    lists();
    mockApi({ "GET /transactions": fail(422, 'unknown field "paye:" (did you mean payee:?)') });
    renderWithProviders(<Search />, { route: "/search?q=paye:x" });
    await waitFor(() =>
      expect(screen.getByText(/Could not load results: unknown field "paye:"/)).toBeInTheDocument(),
    );
  });
});
