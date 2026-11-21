import { describe, expect, it } from "vitest";
import {
  accountTypeLabel,
  amountBasisLabel,
  capitalize,
  humanize,
  labelFor,
  pathLeafLabel,
  RULE_TYPE_LABELS,
} from "./labels";

describe("labels (#541)", () => {
  it("names every account type in words", () => {
    expect(accountTypeLabel("credit_card")).toBe("Credit card");
    expect(accountTypeLabel("checking")).toBe("Checking");
    expect(accountTypeLabel("category_expense")).toBe("Expense category");
    expect(accountTypeLabel(null)).toBe("—");
  });

  it("falls back to words for a value no map knows, never the raw value", () => {
    expect(accountTypeLabel("health_savings")).toBe("Health savings");
    expect(labelFor(RULE_TYPE_LABELS, "some-new_kind")).toBe("Some new kind");
    expect(labelFor(RULE_TYPE_LABELS, "transfer_link")).toBe("Link transfers");
    expect(labelFor(RULE_TYPE_LABELS, "")).toBe("—");
  });

  it("says where a scheduled amount comes from", () => {
    expect(amountBasisLabel("counterparty_balance")).toBe("current balance");
    expect(amountBasisLabel("rolling_avg_3")).toBe("3-payment average");
    expect(amountBasisLabel("future_basis")).toBe("future basis");
    expect(amountBasisLabel(null)).toBe("—");
  });

  it("reads a path's last label as words", () => {
    expect(pathLeafLabel("expenses.dining.coffee_shops")).toBe("Coffee shops");
    expect(pathLeafLabel("equity.securities")).toBe("Securities");
    expect(pathLeafLabel("income")).toBe("Income");
  });

  it("humanizes and capitalizes without touching the rest", () => {
    expect(humanize("wait_for_match")).toBe("Wait for match");
    expect(capitalize("payee contains \"ACME\"")).toBe("Payee contains \"ACME\"");
    expect(capitalize("")).toBe("");
  });
});
