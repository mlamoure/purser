import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../../api/schema";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import SimpleFin from "./SimpleFin";

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
    ...over,
  };
}

function setup(rows: DiscoveredOut[], connection: object = CONNECTION) {
  mockApi({
    "GET /connections": ok([connection]),
    "GET /config": ok(undefined),
    "GET /connections/{connection_id}/discovered": ok(rows),
    "GET /connections/inventory": ok([]),
    "PATCH /accounts/{account_id}": ok({}),
  });
  return renderWithProviders(<SimpleFin />);
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
    expect(
      await screen.findByText("con.auth: Log in again; gen.api: Requested range too long"),
    ).toBeInTheDocument();
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
