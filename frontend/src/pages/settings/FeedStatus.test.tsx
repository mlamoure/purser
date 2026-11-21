import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { meQuery } from "../../lib/me";
import Connections from "./Connections";

const CONNECTION = {
  id: "conn-1",
  provider: "simplefin",
  status: "warning",
  created_at: "2026-06-01T12:00:00Z",
  last_fetch_at: "2026-06-02T12:00:00Z",
  last_errors: [],
  last_issues: [],
  protocol_version: 2,
  fetches_today: 1,
  backfill_cursor: null,
  backfill_complete: true,
};

const account = (over: object) => ({
  name: "Everyday Checking",
  external_id: null,
  account: "assets.checking",
  connected: true,
  closed: false,
  balance_date: "2026-06-02",
  days_since_balance: 0,
  stale: false,
  holdings: 0,
  transactions: 3,
  issue: null,
  ambiguous: false,
  ...over,
});

const INVENTORY = [
  {
    connection_id: null,
    status: "warning",
    last_sync_at: "2026-06-02T12:00:00Z",
    protocol_version: 2,
    stale_days: 4,
    logins: [
      {
        institution: "Example Bank",
        conn_id: null,
        url: null,
        issue: null,
        accounts: [
          account({}),
          account({
            name: "Brokerage",
            account: "assets.brokerage",
            balance_date: "2026-05-20",
            days_since_balance: 13,
            stale: true,
            holdings: 2,
            transactions: 0,
          }),
        ],
      },
      {
        institution: "Other Bank",
        conn_id: null,
        url: null,
        issue: { code: "con.auth", message: "Log in again" },
        accounts: [account({ name: "New Card", account: null, connected: false })],
      },
    ],
  },
];

const CONFIG = {
  values: { sync_schedule: { preset: "daily", hour: 6, minute: 30 }, feed_stale_days: 4 },
  sync_reschedule_live: true,
};

function setup(extra: Record<string, ReturnType<typeof ok> | ((init: unknown) => unknown)> = {}) {
  mockApi({
      "GET /accounts": ok([]),
    "GET /connections": ok([CONNECTION]),
    "GET /config": ok(CONFIG),
    "GET /connections/{connection_id}/discovered": ok([]),
    "GET /connections/inventory": ok(INVENTORY),
    ...(extra as Record<string, ReturnType<typeof ok>>),
  });
  // An admin session: the sync schedule (GET /config) is admin-only (#542).
  const view = renderWithProviders(<Connections />);
  view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);
  return view;
}

describe("Banks and accounts (issues #489, #561)", () => {
  it("shows each bank's status and every account's connection and freshness", async () => {
    setup();
    const card = await screen.findByRole("region", { name: "SimpleFIN connection" });
    // The summary says what is wrong in words, not a bare "warning".
    const summary = await within(card).findByRole("list", { name: "Connection summary" });
    expect(within(summary).getByText("1 of 2 banks needs a new login")).toBeInTheDocument();
    expect(within(summary).getByText("1 account not updated in over 4 days")).toBeInTheDocument();
    expect(within(card).queryByText("warning")).not.toBeInTheDocument();

    // The bank that needs the user is listed first, with what to do.
    const banks = within(card).getAllByRole("rowgroup").filter((g) => g.getAttribute("aria-label"));
    expect(banks.map((g) => g.getAttribute("aria-label"))).toEqual(["Other Bank", "Example Bank"]);
    expect(within(banks[0]).getByText("needs login")).toBeInTheDocument();
    expect(within(banks[0]).getByText(/Log in to this bank again in SimpleFIN Bridge/)).toBeInTheDocument();
    expect(within(banks[0]).getByText("not in Purser yet")).toBeInTheDocument();
    expect(within(banks[1]).getByText("ok")).toBeInTheDocument();
    expect(within(banks[1]).getByText("stale")).toBeInTheDocument();
    expect(within(banks[1]).getByText(/\(13d ago\)/)).toBeInTheDocument();
  });

  it("says so when every bank is connected", async () => {
    setup({
      "GET /connections/inventory": ok([
        { ...INVENTORY[0], logins: [{ ...INVENTORY[0].logins[0], accounts: [account({})] }] },
      ]),
    });
    expect(await screen.findByText("All 1 bank connected")).toBeInTheDocument();
  });
});

describe("Re-fetch a date range (issue #489)", () => {
  it("posts the range and the chosen accounts and reports the result", async () => {
    const { user } = setup({
      "POST /connections/{connection_id}/refetch": ok({
        raw_fetch_id: "rf-1",
        start: "2026-04-01",
        end: "2026-05-01",
        accounts: ["assets.checking"],
        imported: 4,
        skipped: 9,
      }),
    });
    const from = await screen.findByLabelText("From");
    await user.clear(from);
    await user.type(from, "2026-04-01");
    const to = screen.getByLabelText("To");
    await user.clear(to);
    await user.type(to, "2026-05-01");
    await user.selectOptions(screen.getByLabelText("Accounts (none selected = all)"), "assets.checking");
    await user.click(screen.getByRole("button", { name: "Re-fetch" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/refetch", {
      params: { path: { connection_id: "conn-1" } },
      body: { start: "2026-04-01", end: "2026-05-01", accounts: ["assets.checking"] },
    });
    expect(
      await screen.findByText(/Apr 1, 2026 to May 1, 2026: 4 imported, 9 already in the ledger/),
    ).toBeInTheDocument();
  });
});

describe("Sync now reports an outage repair (issue #489)", () => {
  const syncReply = (gap_repair: object) =>
    ok({
      raw_fetch_id: "rf-2",
      imported: 0,
      skipped: 0,
      unmapped_accounts: 0,
      voided_pendings: 0,
      errors: [],
      issues: [],
      gap_repair,
    });

  it("says what it recovered", async () => {
    const { user } = setup({
      "POST /connections/{connection_id}/sync": syncReply({
        accounts: ["assets.checking"],
        start: "2026-03-01",
        end: "2026-05-28",
        unreachable_before: "2026-03-05",
        deferred: null,
        imported: 12,
      }),
    });
    await user.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(
      await screen.findByText(/Recovered from an outage: re-fetched Mar 1, 2026 to May 28, 2026 for assets.checking, 12 transactions imported/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Rows before Mar 5, 2026 are beyond the bank's reach/)).toBeInTheDocument();
  });

  it("says when the repair is postponed", async () => {
    const { user } = setup({
      "POST /connections/{connection_id}/sync": syncReply({
        accounts: ["assets.checking"],
        start: "2026-03-01",
        end: "2026-05-28",
        unreachable_before: null,
        deferred: "rate_budget",
        imported: 0,
      }),
    });
    await user.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(await screen.findByText(/today's fetch budget is spent/)).toBeInTheDocument();
  });
});

describe("Stale threshold setting (issue #489)", () => {
  it("saves feed_stale_days with the schedule", async () => {
    const { user } = setup({ "PUT /config": ok(CONFIG) });
    const days = await screen.findByLabelText("Stale after (days)");
    await user.clear(days);
    await user.type(days, "7");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
      body: { sync_schedule: { preset: "daily", hour: 6, minute: 30 }, feed_stale_days: 7 },
    });
  });

  it("refuses a value outside 1-90 without calling the API", async () => {
    const { user } = setup({ "PUT /config": ok(CONFIG) });
    const days = await screen.findByLabelText("Stale after (days)");
    await user.clear(days);
    await user.type(days, "120");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/whole number of days from 1 to 90/)).toBeInTheDocument();
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });
});
