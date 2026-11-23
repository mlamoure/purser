import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { apiMock, mockApi, ok, fail } from "../test/api";
import { accountOut, transferLinkSuggestion, transferPairSuggestion } from "../test/fixtures";
import { shownText, spokenText } from "../test/a11y";
import { renderWithProviders } from "../test/render";
import { meQuery } from "../lib/me";
import Dashboard from "./Dashboard";

type SuggestionOut = components["schemas"]["SuggestionOut"];
const potential = (overrides = {}) => ({
  as_of: "2026-11-15", owned_assets: "1200", liabilities: "200", owned_net_worth: "1000",
  unvested_value: "500", known_unvested_value: "500", potential_assets: "1700",
  potential_net_worth: "1500", complete: true, unvested_awards: 2,
  unknown_quantity_awards: 0, unsupported_currency_awards: 0, unpriced_symbols: [],
  stale_price_symbols: [], oldest_price_as_of: "2026-11-13", next_vest_date: "2027-01-01",
  ...overrides,
});

type Kind = "match" | "schedule" | "transfer_link" | "transfer_pair" | "split";

/** Everything the Dashboard reads, quiet: no accounts, no bills, nothing to
 * review. The inbox answers like the server: `GET /suggestions/counts` the
 * open total of each kind, and `GET /suggestions?kind=…&limit=…&offset=…`
 * that page of the kind's rows. `GET /matching/transfer-candidates` is
 * deliberately absent — an unrouted call fails the test (src/test/setup.ts). */
function quietDashboard(open: SuggestionOut[] = []) {
  const kinds: Kind[] = ["match", "schedule", "transfer_link", "transfer_pair", "split"];
  const counts = Object.fromEntries(
    kinds.map((kind) => [kind, open.filter((s) => s.kind === kind).length]),
  ) as Record<Kind, number>;
  mockApi({
    "GET /accounts": ok([]),
    "GET /networth/potential": ok(potential()),
    "GET /networth": ok({ net_worth: "0.00", assets: "0.00", liabilities: "0.00" }),
    "GET /suggestions/counts": ok({ total: open.length, ...counts }),
    "GET /suggestions": (init) => {
      const query = init.params?.query ?? {};
      const offset = Number(query.offset ?? 0);
      const limit = Number(query.limit ?? 25);
      return ok(open.filter((s) => s.kind === query.kind).slice(offset, offset + limit));
    },
    "GET /matching/unmatched": ok([]),
    "GET /transactions/review-summary": ok({ total: 0, pending: 0, agent_pending: 0, oldest_date: null, accounts: [] }),
  });
}

/** A detected schedule suggestion, scored. */
const schedule = (n: number): SuggestionOut =>
  transferLinkSuggestion({
    id: `sched-${n}`,
    kind: "schedule",
    source: "detected",
    confidence: "0.900",
    headline: `Bill ${n}: monthly -10.00 on assets.checking with Payee ${n}`,
    draft: {
      version: 1,
      name: `Bill ${n}`,
      account: "assets.checking",
      rrule: "FREQ=MONTHLY;BYMONTHDAY=1",
      dtstart: "2026-10-01",
      amount: "-10.00",
      category: null,
    },
  });

const suggestionCalls = () =>
  apiMock.GET.mock.calls
    .filter(([path]) => path === "/suggestions")
    .map(([, init]) => (init as { params: { query: Record<string, unknown> } }).params.query);

describe("Dashboard — Needs attention", () => {
  it("shows counts first and fetches cards only after expanding a group", async () => {
    quietDashboard([transferPairSuggestion()]);
    renderWithProviders(<Dashboard />);
    const group = await screen.findByRole("button", { name: /Possible transfers/ });
    expect(group).toHaveAttribute("aria-expanded", "false");
    expect(suggestionCalls()).toEqual([]);
    await userEvent.click(group);
    expect(await screen.findByRole("button", { name: "Link as transfer" })).toBeInTheDocument();
    expect(suggestionCalls()).toEqual([{ kind: "transfer_pair", limit: 10, offset: 0, sort: "oldest" }]);
    expect(screen.queryByText("All clear.")).not.toBeInTheDocument();
  });

  it("pages each group and preserves server ordering", async () => {
    quietDashboard([...Array.from({ length: 30 }, (_, n) => schedule(n)), transferPairSuggestion()]);
    renderWithProviders(<Dashboard />);
    await userEvent.click(await screen.findByRole("button", { name: /Scheduled transactions/ }));
    expect(await screen.findByText(/Bill 9: monthly/)).toBeInTheDocument();
    expect(screen.queryByText(/Bill 10: monthly/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show 10 more of 20" }));
    expect(await screen.findByText(/Bill 19: monthly/)).toBeInTheDocument();
    expect(suggestionCalls()).toContainEqual({ kind: "schedule", limit: 10, offset: 10, sort: "oldest" });
    await userEvent.click(screen.getByRole("button", { name: /Possible transfers/ }));
    expect(await screen.findByRole("button", { name: "Link as transfer" })).toBeInTheDocument();
    expect(screen.queryByText(/Bill 19: monthly/)).not.toBeInTheDocument();
  });

  it("shows all clear only when every check succeeds", async () => {
    quietDashboard();
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("All clear.")).toBeInTheDocument();
    expect(suggestionCalls()).toEqual([]);
  });

  it("keeps known counts and exposes a partial failure with retry", async () => {
    quietDashboard([transferPairSuggestion()]);
    mockApi({ "GET /matching/unmatched": fail(503, "Bank check unavailable") });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText(/results below may be incomplete/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Possible transfers/ })).toBeInTheDocument();
    expect(screen.queryByText("All clear.")).not.toBeInTheDocument();
    mockApi({ "GET /matching/unmatched": ok([]) });
    await userEvent.click(screen.getByRole("button", { name: "Retry attention checks" }));
    expect(await screen.findByRole("button", { name: /Possible transfers/ })).toBeInTheDocument();
  });

  it("routes review totals and uncertain expectations to account registers", async () => {
    quietDashboard();
    mockApi({
      "GET /transactions/review-summary": ok({ total: 5, pending: 2, agent_pending: 0, accounts: [
        { account_id: "a1", name: "Checking", total: 5, pending: 2, agent_pending: 0 },
      ] }),
      "GET /matching/unmatched": ok([{ transaction_id: "t1", account_id: "a1", account_name: "Checking", amount: "-20", date: "2026-10-01", aging_days: 2, has_proposed_match: false, amount_estimated: true, schedule_status: "paused", description: "Example utility" }]),
    });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("Estimate — verify details")).toBeInTheDocument();
    // The estimated amount reads "≈ −$20.00", heard as "estimated" (#556).
    const line = screen.getByText(/^Checking ·/);
    expect(shownText(line)).toContain("Checking · \u2248 \u2212$20.00 · expected");
    expect(spokenText(line)).toContain("Checking · estimated \u2212$20.00 · expected");
    expect(screen.getByText("Scheduled transaction paused")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review expectation" })).toHaveAttribute("href", "/accounts/a1?status=expected&start=2026-10-01&end=2026-10-01&transaction=t1");
    await userEvent.click(screen.getByText("Transaction review"));
    expect(screen.getByRole("link", { name: "3 posted" })).toHaveAttribute("href", "/accounts/a1?reviewed=false&status=posted");
    expect(screen.getByRole("link", { name: "2 pending" })).toHaveAttribute("href", "/accounts/a1?reviewed=false&status=pending");
  });
});

describe("Dashboard future stock value", () => {
  it("places potential totals inside the existing cards with expandable award detail", async () => {
    quietDashboard();
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("$1,500.00")).toBeInTheDocument();
    expect(screen.getByText("$1,700.00")).toBeInTheDocument();
    const worth = within(screen.getByLabelText("Net worth"));
    const assets = within(screen.getByLabelText("Assets"));
    expect(worth.getByText("$1,500.00")).toBeInTheDocument();
    expect(assets.getByText("$1,700.00")).toBeInTheDocument();
    expect(worth.getByText("$0.00")).toBeInTheDocument();
    expect(assets.getByText("$0.00")).toBeInTheDocument();
    expect(screen.queryByText("Future value including unvested stock")).not.toBeInTheDocument();
    const detail = assets.getByText("Unvested stock: $500.00").closest("details")!;
    expect(detail).not.toHaveAttribute("open");
    await userEvent.click(assets.getByText("Unvested stock: $500.00"));
    expect(detail).toHaveAttribute("open");
    expect(screen.getByText(/before tax withholding/)).toBeInTheDocument();
    expect(screen.getByText(/Next scheduled vest: Jan 1, 2027/)).toBeInTheDocument();
    expect(screen.getByText("Net worth")).toBeInTheDocument();
  });
  it("keeps missing values unknown and identifies partial and stale valuations", async () => {
    quietDashboard();
    mockApi({ "GET /networth/potential": ok(potential({
      complete: false, unvested_value: null, potential_assets: null, potential_net_worth: null,
      unknown_quantity_awards: 1, unpriced_symbols: ["MISS"], stale_price_symbols: ["OLD"],
    })) });
    renderWithProviders(<Dashboard />);
    expect(await screen.findAllByText("Valuation incomplete")).toHaveLength(2);
    await userEvent.click(screen.getByText(/Unvested stock:/));
    expect(screen.getByText(/Known priced awards/)).toHaveTextContent("$500.00");
    expect(screen.getByText(/Known priced awards/)).toHaveTextContent("MISS");
    expect(screen.getAllByText(/Stale prices: OLD/)).toHaveLength(2);
    expect(screen.queryByText("$1,500.00")).not.toBeInTheDocument();
  });
  it("hides the extra detail entirely when there are no unvested awards", async () => {
    quietDashboard();
    mockApi({ "GET /networth/potential": ok(potential({
      unvested_awards: 0, unvested_value: "0", known_unvested_value: "0",
      potential_assets: "1200", potential_net_worth: "1000",
      oldest_price_as_of: null, next_vest_date: null,
    })) });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("All clear.")).toBeInTheDocument();
    expect(screen.queryByText("Including unvested")).not.toBeInTheDocument();
    expect(screen.queryByText(/Unvested stock/)).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Net worth")).getByText("$0.00")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Assets")).getByText("$0.00")).toBeInTheDocument();
  });
  it("keeps unknown award quantities visible even when the known value is zero", async () => {
    quietDashboard();
    mockApi({ "GET /networth/potential": ok(potential({
      complete: false, unvested_awards: 1, unknown_quantity_awards: 1,
      known_unvested_value: "0", unvested_value: null,
      potential_assets: null, potential_net_worth: null,
    })) });
    renderWithProviders(<Dashboard />);
    expect(await screen.findAllByText("Valuation incomplete")).toHaveLength(2);
    expect(screen.getAllByText("Including unvested")).toHaveLength(2);
  });
  it("shows a read error instead of reporting zero awards", async () => {
    quietDashboard();
    mockApi({ "GET /networth/potential": fail(503, "Award values unavailable") });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("Unvested values unavailable: Award values unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/0 unvested award/)).not.toBeInTheDocument();
  });
});

describe("Dashboard accounts", () => {
  it("shows a failed account load as an error, not as no accounts", async () => {
    quietDashboard();
    mockApi({ "GET /accounts": fail(500, "database unavailable") });
    renderWithProviders(<Dashboard />);

    expect(
      await screen.findByText("Could not load accounts: database unavailable"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No accounts yet/)).not.toBeInTheDocument();
  });

  it("shows liabilities with the same sign as the liability rows (#540)", async () => {
    quietDashboard();
    mockApi({
      "GET /networth": ok({ net_worth: "1120.00", assets: "1500.00", liabilities: "380.00" }),
      "GET /accounts": ok([
        accountOut({
          id: "a-cc",
          name: "Card",
          path: "liabilities.card",
          type: "credit_card",
          balance: "-380.0000",
        }),
      ]),
    });
    renderWithProviders(<Dashboard />);

    const tile = await screen.findByRole("region", { name: "Liabilities" });
    expect(await within(tile).findByText("-$380.00")).toBeInTheDocument();
    const row = (await screen.findByRole("link", { name: "Card" })).closest("tr")!;
    expect(within(row).getByText("-$380.00")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Net worth" })).getByText("$1,120.00")).toBeInTheDocument();
  });

  it("keeps a compact summary: name and balance, and a link to all accounts (#542)", async () => {
    quietDashboard();
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-cc", name: "Rewards Card", path: "liabilities.card", type: "credit_card", balance: "-12.5000" }),
      ]),
    });
    renderWithProviders(<Dashboard />);

    const row = (await screen.findByRole("link", { name: "Rewards Card" })).closest("tr")!;
    expect(within(row).getByRole("link")).toHaveAttribute("href", "/accounts/a-cc");
    expect(within(row).getByText("-$12.50")).toBeInTheDocument();
    expect(within(row).queryByText("liabilities.card")).not.toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Account", "Balance"]);
    expect(screen.getByRole("link", { name: "All accounts" })).toHaveAttribute("href", "/accounts");
    // Hidden and closed accounts are the Accounts page's toggles.
    expect(screen.queryByLabelText("Show hidden")).not.toBeInTheDocument();
  });
});

describe("Dashboard — accounts grouped by kind (#593)", () => {
  const mixed = [
    accountOut({ id: "a-loan", name: "Mortgage", path: "liabilities.mortgage", type: "loan", balance: "-250000.0000" }),
    accountOut({ id: "a-chk", name: "Checking", path: "assets.checking", type: "checking", balance: "1200.5000" }),
    accountOut({ id: "a-brk", name: "Brokerage", path: "assets.brokerage", type: "brokerage", balance: "5000.0000" }),
    accountOut({ id: "a-sav", name: "Savings", path: "assets.savings", type: "savings", balance: "800.2500" }),
    accountOut({
      id: "a-house", name: "House", path: "assets.house", type: "property", balance: "400000.0000",
      equity: "150000.0000", secured_loan_ids: ["a-loan"],
    }),
    accountOut({ id: "a-cc", name: "Card", path: "liabilities.card", type: "credit_card", balance: "-99.9900" }),
  ];

  beforeEach(() => window.localStorage.clear());

  it("orders groups Banking, Credit cards, Investments, Property, Loans with exact subtotals", async () => {
    quietDashboard();
    mockApi({ "GET /accounts": ok(mixed) });
    renderWithProviders(<Dashboard />);

    await screen.findByRole("link", { name: "Mortgage" });
    const groups = screen.getAllByRole("button", { expanded: true }).map((b) => b.textContent);
    expect(groups).toEqual([
      "Banking· 2",
      "Credit cards· 1",
      "Investments· 1",
      "Property· 1",
      "Loans· 1",
    ]);
    const banking = screen.getByRole("rowgroup", { name: "Banking" });
    // 1200.50 + 800.25, added as decimals.
    expect(within(banking).getByText("$2,000.75")).toBeInTheDocument();
    expect(within(banking).getByRole("link", { name: "Checking" })).toBeInTheDocument();
    expect(within(banking).getByRole("link", { name: "Savings" })).toBeInTheDocument();
    const loans = screen.getByRole("rowgroup", { name: "Loans" });
    expect(within(loans).getAllByText("-$250,000.00")).toHaveLength(2); // subtotal and row
  });

  it("shows a secured asset's equity under its name", async () => {
    quietDashboard();
    mockApi({ "GET /accounts": ok(mixed) });
    renderWithProviders(<Dashboard />);

    const property = await screen.findByRole("rowgroup", { name: "Property" });
    expect(within(property).getByText("Equity $150,000.00")).toBeInTheDocument();
  });

  it("collapses a group to its subtotal and remembers it", async () => {
    quietDashboard();
    mockApi({ "GET /accounts": ok(mixed) });
    const { unmount } = renderWithProviders(<Dashboard />);

    const toggle = await screen.findByRole("button", { name: /^Banking/ });
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Checking" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("rowgroup", { name: "Banking" })).getByText("$2,000.75")).toBeInTheDocument();

    unmount();
    renderWithProviders(<Dashboard />);
    expect(await screen.findByRole("button", { name: /^Banking/ })).toHaveAttribute("aria-expanded", "false");
    // Other groups stay open.
    expect(screen.getByRole("link", { name: "Card" })).toBeInTheDocument();
  });

  it("puts an unknown account type in Other rather than dropping it", async () => {
    quietDashboard();
    mockApi({
      "GET /accounts": ok([accountOut({ id: "a-x", name: "Mystery", path: "assets.mystery", type: "crypto_wallet", balance: "1.0000" })]),
    });
    renderWithProviders(<Dashboard />);

    const other = await screen.findByRole("rowgroup", { name: "Other" });
    expect(within(other).getByRole("link", { name: "Mystery" })).toBeInTheDocument();
  });
});

describe("Dashboard — first-run checklist (#539)", () => {
  it("shows the checklist to an admin with setup left to do", async () => {
    quietDashboard();
    mockApi({
      "GET /config": ok({
        encryption: {
          field_encryption_active: true,
          canary_ok: true,
          key_fingerprint: "abcd1234",
          key_acknowledged_at: null,
          encrypted_scopes: [],
        },
      }),
      "GET /connections": ok([]),
      "GET /tokens": ok([]),
    });
    const view = renderWithProviders(<Dashboard />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);

    const card = await screen.findByRole("region", { name: "Finish setting up" });
    expect(within(card).getByRole("link", { name: "Show and record the key" })).toBeInTheDocument();
  });

  it("never reads setup state for a non-admin session", async () => {
    quietDashboard();
    const view = renderWithProviders(<Dashboard />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write"] } as never);

    await screen.findByText(/No accounts yet/);
    expect(screen.queryByRole("region", { name: "Finish setting up" })).not.toBeInTheDocument();
    // An unrouted GET /config, /connections or /tokens would fail the test.
  });
});
