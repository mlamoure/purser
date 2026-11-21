/* The string-based money formatter (#540). Every input is a decimal string,
 * as the API sends it: no test value passes through a float (guardrail 1). */

import { describe, expect, it } from "vitest";
import { compareMoney, formatMoney, moneyEditText, moneySign, negateMoney } from "./money";

describe("formatMoney", () => {
  it("formats sign, grouping and cents", () => {
    expect(formatMoney("1234.5")).toBe("$1,234.50");
    expect(formatMoney("-380.00")).toBe("-$380.00");
    expect(formatMoney("0")).toBe("$0.00");
    expect(formatMoney("0.0000")).toBe("$0.00");
    expect(formatMoney("+12")).toBe("$12.00");
    expect(formatMoney(".5")).toBe("$0.50");
    expect(formatMoney("007.10")).toBe("$7.10");
  });

  it("groups large values without losing digits", () => {
    expect(formatMoney("9999999999999999.9999")).toBe("$10,000,000,000,000,000.00");
    expect(formatMoney("1234567890123456.7812")).toBe("$1,234,567,890,123,456.78");
    expect(formatMoney("-1000000")).toBe("-$1,000,000.00");
    expect(formatMoney("100")).toBe("$100.00");
  });

  it("rounds half-up on the digit string, away from zero", () => {
    expect(formatMoney("1.005")).toBe("$1.01");
    expect(formatMoney("-1.005")).toBe("-$1.01");
    expect(formatMoney("1.0049")).toBe("$1.00");
    expect(formatMoney("2.675")).toBe("$2.68"); // 2.67 through a binary float
    expect(formatMoney("999.995")).toBe("$1,000.00");
    expect(formatMoney("-0.995")).toBe("-$1.00");
  });

  it("formats 4-decimal ledger values (NUMERIC(20,4))", () => {
    expect(formatMoney("12.3456")).toBe("$12.35");
    expect(formatMoney("12.3449")).toBe("$12.34");
    expect(formatMoney("-1500.0000")).toBe("-$1,500.00");
  });

  it("shows no minus sign on a value that rounds to zero", () => {
    expect(formatMoney("-0.001")).toBe("$0.00");
    expect(formatMoney("-0.0000")).toBe("$0.00");
  });

  it("uses the value's currency, USD when none is given", () => {
    expect(formatMoney("12.5", { currency: "EUR" })).toBe("€12.50");
    expect(formatMoney("-12.5", { currency: "GBP" })).toBe("-£12.50");
    expect(formatMoney("12.5", { currency: "CAD" })).toBe("CA$12.50");
    expect(formatMoney("12.5", { currency: "CHF" })).toBe("CHF 12.50");
    expect(formatMoney("12.5", { currency: "usd" })).toBe("$12.50");
    expect(formatMoney("12.5", { currency: null })).toBe("$12.50");
    // No minor unit: rounded half-up to whole yen.
    expect(formatMoney("1234.5", { currency: "JPY" })).toBe("¥1,235");
    // A malformed code shows as itself rather than failing.
    expect(formatMoney("5", { currency: "Q1" })).toBe("Q1 5.00");
  });

  it("shows the sign as asked", () => {
    expect(formatMoney("5", { signDisplay: "always" })).toBe("+$5.00");
    expect(formatMoney("-5", { signDisplay: "always" })).toBe("-$5.00");
    expect(formatMoney("0", { signDisplay: "always" })).toBe("$0.00");
    expect(formatMoney("-5", { signDisplay: "never" })).toBe("$5.00");
  });

  it("shows a dash for no value and a malformed value as is", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
    expect(formatMoney("")).toBe("—");
    expect(formatMoney("1e5")).toBe("1e5");
    expect(formatMoney("abc")).toBe("abc");
  });
});

describe("moneySign, negateMoney, compareMoney", () => {
  it("reads the sign from the string", () => {
    expect(moneySign("-0.0001")).toBe(-1);
    expect(moneySign("0.0000")).toBe(0);
    expect(moneySign("-0")).toBe(0);
    expect(moneySign("12")).toBe(1);
    expect(moneySign(null)).toBe(0);
  });

  it("flips the sign, keeping the digits", () => {
    expect(negateMoney("380.00")).toBe("-380.00");
    expect(negateMoney("-380.0000")).toBe("380.0000");
    expect(negateMoney("-0.00")).toBe("0.00");
    expect(negateMoney("0")).toBe("0");
  });

  it("compares exactly, beyond float precision", () => {
    expect(compareMoney("0.05", "0.5")).toBeLessThan(0);
    expect(compareMoney("10", "9.9999")).toBeGreaterThan(0);
    expect(compareMoney("-10", "-9.9999")).toBeLessThan(0);
    expect(compareMoney("1.50", "1.5000")).toBe(0);
    expect(compareMoney("-0", "0")).toBe(0);
    expect(compareMoney("-1", "0.0001")).toBeLessThan(0);
    // Equal as floats, different as decimals.
    expect(compareMoney("9007199254740993.0001", "9007199254740993.0000")).toBeGreaterThan(0);
  });
});

describe("moneyEditText", () => {
  it("drops trailing zeros past the cents without rounding", () => {
    expect(moneyEditText("500.0000")).toBe("500.00");
    expect(moneyEditText("12.3450")).toBe("12.345");
    expect(moneyEditText("-7")).toBe("-7.00");
  });
});
