import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../../api/schema";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { meQuery } from "../../lib/me";
import { configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Connections from "./Connections";

type DiscoveredOut = components["schemas"]["DiscoveredOut"];

const CONNECTION = {
  id: "conn-1",
  provider: "simplefin",
  status: "active",
  created_at: "2026-06-01T12:00:00Z",
  last_fetch_at: "2026-06-02T12:00:00Z",
  last_errors: [],
  last_issues: [],
  protocol_version: 2,
  fetches_today: 1,
  backfill_cursor: null,
  backfill_complete: true,
};

function row(over: Partial<DiscoveredOut>): DiscoveredOut {
  return {
    external_id: "ext-card",
    name: "Synthetic Card",
    org: "Example Bank",
    currency: "USD",
    balance: "-535.00",
    balance_date: null,
    mapped_account_id: "acct-card",
    mapped_account_path: "liabilities.card",
    ledger_balance: "-523.0000",
    drift: "-12.0000",
    drift_basis: "pending_inclusive",
    drift_basis_pinned: false,
    drift_basis_learned: "pending_inclusive",
    ambiguous: false,
    ignored: false,
    ...over,
  };
}

function setup(rows: DiscoveredOut[], connection: object = CONNECTION) {
  mockApi({
      "GET /accounts": ok([]),
    "GET /connections": ok([connection]),
    "GET /config": ok(undefined),
    "GET /connections/{connection_id}/discovered": ok(rows),
    "GET /connections/inventory": ok([]),
    "PATCH /accounts/{account_id}": ok({}),
  });
  return renderAs(["read", "write", "admin"]);
}

function renderAs(scopes: string[]) {
  const view = renderWithProviders(<Connections />);
  view.queryClient.setQueryData(meQuery.queryKey, { scopes } as never);
  return view;
}

describe("SimpleFin feed accounts: drift-basis pin (issue #307)", () => {
  it("marks a pinned basis and shows the learned one under Auto", async () => {
    setup([
      row({
        drift_basis: "pending_inclusive",
        drift_basis_pinned: true,
        drift_basis_learned: "posted_only",
      }),
      row({
        external_id: "ext-loan",
        name: "Synthetic Loan",
        mapped_account_id: "acct-loan",
        mapped_account_path: "liabilities.loan",
        ledger_balance: null,
        drift: null,
        drift_basis: null,
        drift_basis_learned: null,
      }),
    ]);
    const select = await screen.findByRole("combobox", {
      name: "Drift basis for liabilities.card",
    });
    expect(select).toHaveValue("pending_inclusive");
    expect(within(select).getByRole("option", { name: "Auto (learned: posted only)" })).toBeInTheDocument();
    expect(screen.getByText("pinned")).toBeInTheDocument();
    // The drift check never runs on the loan: no basis control for it.
    expect(
      screen.queryByRole("combobox", { name: "Drift basis for liabilities.loan" }),
    ).not.toBeInTheDocument();
  });

  it("pins a basis through PATCH /accounts", async () => {
    const { user } = setup([row({ drift_basis_learned: "pending_inclusive" })]);
    const select = await screen.findByRole("combobox", {
      name: "Drift basis for liabilities.card",
    });
    expect(select).toHaveValue("auto");
    expect(screen.queryByText("pinned")).not.toBeInTheDocument();

    await user.selectOptions(select, "posted_only");
    expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
      params: { path: { account_id: "acct-card" } },
      body: { drift_basis_override: "posted_only" },
    });
  });

  it("clears a pin by sending null", async () => {
    const { user } = setup([
      row({ drift_basis: "posted_only", drift_basis_pinned: true, drift_basis_learned: "posted_only" }),
    ]);
    const select = await screen.findByRole("combobox", {
      name: "Drift basis for liabilities.card",
    });
    expect(select).toHaveValue("posted_only");

    await user.selectOptions(select, "auto");
    expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
      params: { path: { account_id: "acct-card" } },
      body: { drift_basis_override: null },
    });
  });
});

describe("SimpleFin connection errors (issue #479)", () => {
  it("shows each v2 error with its code", async () => {
    setup([], {
      ...CONNECTION,
      status: "warning",
      last_errors: ["Log in again", "Requested range too long"],
      last_issues: [
        { code: "con.auth", msg: "Log in again", conn_id: "CON-1", account_id: null },
        { code: "gen.api", msg: "Requested range too long", conn_id: null, account_id: null },
      ],
    });
    // No bank row carries them here, so both are listed in full.
    expect(await screen.findByText("con.auth: Log in again")).toBeInTheDocument();
    expect(screen.getByText("gen.api: Requested range too long")).toBeInTheDocument();
  });

  it("shows a v1 message without a code", async () => {
    setup([], {
      ...CONNECTION,
      status: "warning",
      last_errors: ["Connection to Example Bank may need attention"],
      last_issues: [
        {
          code: "gen.",
          msg: "Connection to Example Bank may need attention",
          conn_id: null,
          account_id: null,
        },
      ],
      protocol_version: 1,
    });
    expect(
      await screen.findByText("Connection to Example Bank may need attention"),
    ).toBeInTheDocument();
  });
});

describe("SimpleFin feed accounts: map after setup (#539)", () => {
  const unmapped = row({
    external_id: "ext-new",
    name: "New Savings",
    balance: "300.00",
    mapped_account_id: null,
    mapped_account_path: null,
    ledger_balance: null,
    drift: null,
    drift_basis: null,
    drift_basis_learned: null,
  });

  function setupMapping(admin: boolean) {
    let mapped = false;
    mockApi({
      "GET /connections": ok([CONNECTION]),
      "GET /config": ok(undefined),
      "GET /connections/{connection_id}/discovered": () =>
        ok([
          mapped
            ? { ...unmapped, mapped_account_id: "acct-new", mapped_account_path: "assets.new_savings" }
            : unmapped,
        ]),
      "GET /connections/inventory": ok([]),
      "GET /accounts": ok([]),
      "POST /connections/{connection_id}/map": () => {
        mapped = true;
        return ok({ account_id: "acct-new", path: "assets.new_savings" });
      },
    });
    return renderAs(admin ? ["read", "write", "admin"] : ["read", "write"]);
  }

  it("maps an unmapped account to a new account and refreshes the list", async () => {
    const { user } = setupMapping(true);
    const tr = (await screen.findByText("New Savings")).closest("tr")!;
    expect(within(tr).getByText("not in Purser yet")).toBeInTheDocument();

    await user.click(within(tr).getByRole("button", { name: "Map New Savings" }));
    await user.selectOptions(screen.getByLabelText("Account type"), "savings");
    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/map", {
      params: { path: { connection_id: "conn-1" } },
      body: {
        external_id: "ext-new",
        create: { name: "New Savings", type: "savings", path: "assets.new_savings", currency: "USD" },
      },
    });
    // The list refetches and shows the mapping; the form is gone.
    expect(await screen.findByText("assets.new_savings")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Map account" })).not.toBeInTheDocument();
    const discoveredCalls = apiMock.GET.mock.calls.filter(
      ([path]) => path === "/connections/{connection_id}/discovered",
    );
    expect(discoveredCalls.length).toBeGreaterThanOrEqual(2);
  });

  it("shows the server's reason when mapping fails", async () => {
    const { user } = setupMapping(true);
    mockApi({
      "GET /accounts": ok([]),
      "POST /connections/{connection_id}/map": fail(422, "account path 'assets.new_savings' already exists"),
    });
    await user.click(await screen.findByRole("button", { name: "Map New Savings" }));
    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(
      await screen.findByText("account path 'assets.new_savings' already exists"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Map account" })).toBeInTheDocument();
  });

  it("explains Map and Ignore while an account is waiting", async () => {
    setupMapping(true);
    expect(await screen.findByText(/One bank account is not in Purser yet/)).toBeInTheDocument();
    expect(screen.queryByText(/REST API/)).not.toBeInTheDocument();
  });
});

describe("SimpleFin feed accounts: ignore (#560)", () => {
  const mapped = row({ name: "Joint Brokerage", external_id: "ext-a", drift: null });
  const twin = row({
    name: "Joint Brokerage",
    external_id: "ext-b",
    mapped_account_id: null,
    mapped_account_path: null,
    ledger_balance: null,
    drift: null,
    drift_basis: null,
  });

  it("lists a waiting account first, flags a likely duplicate and ignores it", async () => {
    const { user } = setup([mapped, twin]);
    let ignored = false;
    mockApi({
      "GET /accounts": ok([]),
      "GET /connections/{connection_id}/discovered": () =>
        ok([mapped, { ...twin, ignored }]),
      "POST /connections/{connection_id}/ignore": () => {
        ignored = true;
        return ok({ external_id: "ext-b", ignored: true });
      },
    });
    await screen.findByText(/Looks like a duplicate of liabilities.card/);
    const rows = screen.getAllByRole("row").filter((r) => within(r).queryByText(/Joint Brokerage/));
    expect(within(rows[0]).getByText("not in Purser yet")).toBeInTheDocument(); // waiting first

    await user.click(screen.getByRole("button", { name: "Ignore Joint Brokerage" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/ignore", {
      params: { path: { connection_id: "conn-1" } },
      body: { external_id: "ext-b", ignored: true },
    });
    expect(await screen.findByText("ignored")).toBeInTheDocument();
    expect(screen.queryByText(/not in Purser yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Looks like a duplicate/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Track Joint Brokerage again" })).toBeInTheDocument();
  });

  it("opens the Map form of the account a ?map= link names", async () => {
    mockApi({
      "GET /connections": ok([CONNECTION]),
      "GET /config": ok(undefined),
      "GET /connections/{connection_id}/discovered": ok([mapped, twin]),
      "GET /connections/inventory": ok([]),
      "GET /accounts": ok([]),
    });
    const view = renderWithProviders(<Connections />, { route: "/?map=ext-b" });
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);
    expect(await screen.findByRole("form", { name: "Map Joint Brokerage" })).toBeInTheDocument();
  });
});

describe("Connections for a member session (#547, #542)", () => {
  it("syncs and shows bank health without calling the admin-only endpoints", async () => {
    mockApi({
      "GET /accounts": ok([]),
      "GET /connections": ok([CONNECTION]),
      "GET /connections/inventory": ok([]),
    });
    renderAs(["read", "write"]);

    expect(await screen.findByRole("button", { name: "Sync now" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Re-fetch a date range" })).toBeInTheDocument();
    expect(screen.queryByText("Compared with Purser")).not.toBeInTheDocument();
    expect(screen.queryByText("Add a SimpleFIN connection")).not.toBeInTheDocument();
    expect(screen.queryByText("Sync schedule")).not.toBeInTheDocument();
    // GET /config and /discovered are admin-scoped: an unrouted call would
    // fail this test, and none was made.
    const paths = apiMock.GET.mock.calls.map(([path]) => path);
    expect(paths).not.toContain("/config");
    expect(paths).not.toContain("/connections/{connection_id}/discovered");
  });
});

describe("Connections: the daily sync time is a household clock time (#556)", () => {
  function setupSchedule(timezone: string, hour: number, minute = 30) {
    const config = configOut({ sync_schedule: { preset: "daily", hour, minute }, timezone });
    mockApi({
      "GET /accounts": ok([]),
      "GET /connections": ok([]),
      "GET /connections/inventory": ok([]),
      "GET /config": ok(config),
      "PUT /config": ok(config),
    });
    return renderAs(["read", "write", "admin"]);
  }

  async function saveTime(user: ReturnType<typeof setupSchedule>["user"], time: string) {
    const input = await screen.findByLabelText(/^Time \(/);
    await user.clear(input);
    await user.type(input, time);
    const card = screen.getByRole("heading", { name: "Sync schedule" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
  }

  it("shows and saves the stored time as is, labelled with the household zone", async () => {
    const { user } = setupSchedule("America/New_York", 6);
    expect(await screen.findByLabelText("Time (America/New_York)")).toHaveValue("06:30");
    expect(screen.getByText(/stays the same on the clock/)).toBeInTheDocument();
    expect(screen.queryByText(/UTC/)).not.toBeInTheDocument();

    await saveTime(user, "07:15");
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { sync_schedule: { preset: "daily", hour: 7, minute: 15 }, feed_stale_days: 4 },
      }),
    );
  });

  it("needs no daylight-saving note when the household zone is UTC", async () => {
    const { user } = setupSchedule("UTC", 6);
    expect(await screen.findByLabelText("Time (UTC)")).toHaveValue("06:30");
    expect(screen.queryByText(/stays the same on the clock/)).not.toBeInTheDocument();
    await saveTime(user, "05:00");
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { sync_schedule: { preset: "daily", hour: 5, minute: 0 }, feed_stale_days: 4 },
      }),
    );
  });
});
