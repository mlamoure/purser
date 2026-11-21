import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, ok } from "../../test/api";
import { accountOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Rules from "./Rules";

const rule = (over: Record<string, unknown>) => ({
  id: "r-1",
  type: "categorize",
  state: "active",
  priority: 100,
  predicate: 'account in assets.checking AND payee contains "acme"',
  action: "-> expenses.fitness",
  applied_count: 4,
  note: null,
  window_days: null,
  payee_scope: null,
  ...over,
});

describe("Settings, Rules: rules read as sentences (#541)", () => {
  it("shows the rule in words with names, and keeps the stored form", async () => {
    mockApi({
      "GET /rules": ok([
        rule({}),
        rule({
          id: "r-2",
          type: "transfer_link",
          predicate: "legs <= 3 days apart",
          action: "link assets.checking <-> liabilities.card",
          window_days: 3,
        }),
      ]),
      "GET /categories": ok([
        accountOut({ id: "c-1", name: "Fitness", path: "expenses.fitness", type: "category_expense", is_virtual: true }),
      ]),
      "GET /accounts": ok([
        accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
        accountOut({ id: "a-2", name: "Rewards Card", path: "liabilities.card", type: "credit_card" }),
      ]),
    });
    renderWithProviders(<Rules />);

    const row = (await screen.findByText("Categorize")).closest("tr")!;
    expect(await within(row).findByText('On Checking, payee contains "acme"')).toBeInTheDocument();
    expect(await within(row).findByText("Category Fitness")).toBeInTheDocument();
    const stored = within(row).getByText("As stored").closest("details")!;
    expect(stored).toHaveTextContent('account in assets.checking AND payee contains "acme" -> expenses.fitness');

    const link = screen.getByText("Link transfers").closest("tr")!;
    expect(within(link).getByText("Sides within 3 days")).toBeInTheDocument();
    expect(await within(link).findByText("Link transfers between Checking and Rewards Card")).toBeInTheDocument();
    expect(screen.queryByText("transfer_link")).not.toBeInTheDocument();
  });
});
