import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import CategorySelect, { groupCategories } from "./CategorySelect";

const category = (path: string, name: string) =>
  accountOut({ id: `c-${path}`, path, name, type: "category_expense", is_virtual: true });

const CATEGORIES = [
  category("expenses", "Expenses"),
  category("expenses.food", "Food & drink"),
  category("expenses.food.groceries", "Groceries"),
  category("expenses.food.restaurants", "Restaurants"),
  category("expenses.fitness", "Fitness"),
  category("income", "Income"),
  category("income.salary", "Salary & wages"),
];

const ACCOUNTS = [
  accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
  accountOut({ id: "a-2", name: "Rainy Day", path: "assets.savings", type: "savings" }),
];

function renderSelect(value: string, onChange = vi.fn()) {
  mockApi({ "GET /categories": ok(CATEGORIES), "GET /accounts": ok(ACCOUNTS) });
  const view = renderWithProviders(
    <CategorySelect value={value} currentAccountPath="assets.checking" onChange={onChange} />,
  );
  return { ...view, onChange };
}

describe("groupCategories (#541)", () => {
  it("puts each category with children at the head of its own group", () => {
    expect(groupCategories(CATEGORIES)).toEqual([
      { label: "Expenses", options: [{ path: "expenses", name: "Expenses" }, { path: "expenses.fitness", name: "Fitness" }] },
      {
        label: "Food & drink",
        options: [
          { path: "expenses.food", name: "Food & drink" },
          { path: "expenses.food.groceries", name: "Groceries" },
          { path: "expenses.food.restaurants", name: "Restaurants" },
        ],
      },
      { label: "Income", options: [{ path: "income", name: "Income" }, { path: "income.salary", name: "Salary & wages" }] },
    ]);
  });

  it("labels a deeper group with its ancestors below the root", () => {
    const groups = groupCategories([
      category("expenses", "Expenses"),
      category("expenses.home", "Home"),
      category("expenses.home.repairs", "Repairs"),
      category("expenses.home.repairs.roof", "Roof"),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Expenses", "Home", "Home › Repairs"]);
  });
});

describe("CategorySelect (#541)", () => {
  it("shows category names in groups and transfer targets by account name", async () => {
    renderSelect("expenses.food.groceries");
    const select = screen.getByRole("combobox", { name: "Category" });
    const food = await within(select).findByRole("group", { name: "Food & drink" });
    expect(within(food).getByRole("option", { name: "Groceries" })).toBeInTheDocument();
    const transfers = await within(select).findByRole("group", { name: "Transfers" });
    expect(await within(transfers).findByRole("option", { name: "Transfer to Rainy Day" })).toBeInTheDocument();
    // The register's own account is not a transfer target.
    expect(within(transfers).queryByRole("option", { name: "Transfer to Checking" })).not.toBeInTheDocument();
    expect(screen.queryByText(/expenses\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/\[/)).not.toBeInTheDocument();
    expect(select).toHaveValue("expenses.food.groceries");
  });

  it("sends paths: a category path, or [path] for a transfer", async () => {
    const { user, onChange } = renderSelect("expenses.food.groceries");
    const select = screen.getByRole("combobox", { name: "Category" });
    await within(select).findByRole("option", { name: "Fitness" });
    await user.selectOptions(select, "Fitness");
    expect(onChange).toHaveBeenLastCalledWith("expenses.fitness");
    await within(select).findByRole("option", { name: "Transfer to Rainy Day" });
    await user.selectOptions(select, "Transfer to Rainy Day");
    expect(onChange).toHaveBeenLastCalledWith("[assets.savings]");
  });

  it("keeps a value neither list offers, named rather than raw", async () => {
    renderSelect("[liabilities.old_card]");
    const select = screen.getByRole("combobox", { name: "Category" });
    await within(select).findByRole("option", { name: "Groceries" });
    expect(within(select).getByRole("option", { name: "Transfer to Old card" })).toBeInTheDocument();
    expect(select).toHaveValue("[liabilities.old_card]");
  });
});
