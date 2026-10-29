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
    ...over,
  };
}

function setup(rows: DiscoveredOut[]) {
  mockApi({
    "GET /connections": ok([CONNECTION]),
    "GET /config": ok(undefined),
    "GET /connections/{connection_id}/discovered": ok(rows),
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
