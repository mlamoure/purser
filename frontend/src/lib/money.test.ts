/* The string-based money formatter (#540). Every input is a decimal string,
 * as the API sends it: no test value passes through a float (guardrail 1). */

import { describe, expect, it } from "vitest";
import {
  compareMoney,
  formatEstimate,
  formatMoney,
  formatMoneyCompact,
  moneyEditText,
  moneySign,
  negateMoney,
  sumMoney,
  typographicMinus,
} from "./money";

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

  it("rounds to the places asked instead of the currency's minor units", () => {
    expect(formatMoney("1234.5", { places: 0 })).toBe("$1,235");
    expect(formatMoney("-0.5", { places: 0 })).toBe("-$1");
    expect(formatMoney("-0.4", { places: 0 })).toBe("$0");
    expect(formatMoney("12.3456", { places: 4 })).toBe("$12.3456");
    expect(formatMoney("12.5", { currency: "EUR", places: 0 })).toBe("€13");
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

describe("formatMoneyCompact (#594)", () => {
  it("shortens on the digit string", () => {
    expect(formatMoneyCompact("950.4900")).toBe("$950");
    expect(formatMoneyCompact("999.5")).toBe("$1k");
    expect(formatMoneyCompact("999999.99")).toBe("$1M");
    expect(formatMoneyCompact("12345.6789")).toBe("$12.3k");
    expect(formatMoneyCompact("12000")).toBe("$12k");
    expect(formatMoneyCompact("-1250000.00")).toBe("-$1.25M");
    expect(formatMoneyCompact("4000000000")).toBe("$4B");
    expect(formatMoneyCompact("1999.95")).toBe("$2k");
  });

  it("drops the sign of a value that rounds to zero and passes bad input through", () => {
    expect(formatMoneyCompact("-0.4")).toBe("$0");
    expect(formatMoneyCompact(null)).toBe("—");
    expect(formatMoneyCompact("abc")).toBe("abc");
    expect(formatMoneyCompact("1500", { currency: "EUR" })).toBe("€1.5k");
  });
});

describe("sumMoney (#593)", () => {
  it("adds exactly, at the widest fraction", () => {
    expect(sumMoney(["0.1", "0.2"])).toBe("0.3"); // 0.30000000000000004 through floats
    expect(sumMoney(["12.50", "-0.0001"])).toBe("12.4999");
    expect(sumMoney(["-380.0000", "1500.00"])).toBe("1120.0000");
    expect(sumMoney(["9999999999999999.9999", "0.0001"])).toBe("10000000000000000.0000");
  });

  it("handles signs, zero and empty input", () => {
    expect(sumMoney([])).toBe("0");
    expect(sumMoney(["-5", "5"])).toBe("0");
    expect(sumMoney(["-0.25", "-0.5"])).toBe("-0.75");
    expect(sumMoney(["-0.0500"])).toBe("-0.0500");
  });

  it("counts null and malformed values as zero", () => {
    expect(sumMoney([null, undefined, "abc", "2.00"])).toBe("2.00");
  });
});

describe("formatEstimate (#556)", () => {
  it("marks an estimate with U+2248, a space and a real minus sign", () => {
    expect(formatEstimate("-95.00")).toBe("\u2248 \u2212$95.00");
    expect(formatEstimate("-1234.565")).toBe("\u2248 \u2212$1,234.57");
    expect(formatEstimate("1200")).toBe("\u2248 $1,200.00");
    expect(formatEstimate("12", { currency: "GBP" })).toBe("\u2248 \u00a312.00");
    expect(formatEstimate("-0.001")).toBe("\u2248 $0.00"); // no minus on a zero
    expect(formatEstimate("5", { signDisplay: "always" })).toBe("\u2248 +$5.00");
  });

  it("leaves a missing amount unmarked", () => {
    expect(formatEstimate(null)).toBe("\u2014");
    expect(formatEstimate("")).toBe("\u2014");
  });

  it("turns only a leading hyphen into a minus sign", () => {
    expect(typographicMinus("-$5.00")).toBe("\u2212$5.00");
    expect(typographicMinus("$5.00")).toBe("$5.00");
    expect(typographicMinus("Card -1")).toBe("Card -1");
  });
});

