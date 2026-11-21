import { describe, expect, it } from "vitest";
import { accountOut } from "../test/fixtures";
import { categoryValueLabel, namesFrom } from "./names";

const name = namesFrom([
  [accountOut({ id: "c-1", name: "Groceries", path: "expenses.food.groceries", is_virtual: true })],
  [accountOut({ id: "a-2", name: "Rainy Day", path: "assets.savings" })],
]);

describe("path names (#541)", () => {
  it("names a category or an account by its path", () => {
    expect(name("expenses.food.groceries")).toBe("Groceries");
    expect(name("assets.savings")).toBe("Rainy Day");
  });

  it("reads a path no list knows as its last label, and nothing as a dash", () => {
    expect(name("expenses.home_office")).toBe("Home office");
    expect(name(null)).toBe("—");
    expect(name("")).toBe("—");
  });

  it("reads a category cell value, transfer targets included", () => {
    expect(categoryValueLabel("expenses.food.groceries", name)).toBe("Groceries");
    expect(categoryValueLabel("[assets.savings]", name)).toBe("Transfer to Rainy Day");
    expect(categoryValueLabel("", name)).toBe("—");
  });
});
