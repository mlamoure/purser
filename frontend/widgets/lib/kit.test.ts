/// <reference types="vite/client" />
/* Widget money formatting (#550): the same cases as the admin SPA's
 * formatMoney (src/lib/money.test.ts), in the widgets' compact style — a
 * typographic minus, and cents only under 1,000 when the rounded amount is
 * not whole (or when asked). Every input is a decimal string: no test value
 * passes through a float (guardrail 1). */

import { describe, expect, it } from "vitest";
import { shownText, spokenText } from "../../src/test/a11y";
import css from "./theme.css?raw";
import { estimate, estimateNode, money, moneyCompact } from "./kit";

describe("money", () => {
  it("formats sign, grouping and cents", () => {
    expect(money("1234.5")).toBe("$1,235");
    expect(money("1234.5", { cents: true })).toBe("$1,234.50");
    expect(money("-380.00")).toBe("−$380");
    expect(money("17.99")).toBe("$17.99");
    expect(money("0")).toBe("$0");
    expect(money("0.0000")).toBe("$0");
    expect(money("+12")).toBe("$12");
    expect(money(".5")).toBe("$0.50");
    expect(money("007.10")).toBe("$7.10");
  });

  it("groups large values without losing digits", () => {
    expect(money("9999999999999999.9999")).toBe("$10,000,000,000,000,000");
    expect(money("1234567890123456.7812", { cents: true })).toBe("$1,234,567,890,123,456.78");
    expect(money("-1000000")).toBe("−$1,000,000");
    // 2^53 + 1: a float would show ...992.
    expect(money("9007199254740993")).toBe("$9,007,199,254,740,993");
  });

  it("rounds half-up on the digit string, away from zero", () => {
    expect(money("1.005")).toBe("$1.01");
    expect(money("-1.005")).toBe("−$1.01");
    expect(money("1.0049")).toBe("$1");
    expect(money("1.0049", { cents: true })).toBe("$1.00");
    expect(money("2.675")).toBe("$2.68"); // 2.67 through a binary float
    expect(money("999.995")).toBe("$1,000");
    expect(money("-0.995")).toBe("−$1");
    expect(money("2500.5")).toBe("$2,501");
    expect(money("-2500.5")).toBe("−$2,501");
  });

  it("formats 4-decimal ledger values (NUMERIC(20,4))", () => {
    expect(money("12.3456")).toBe("$12.35");
    expect(money("12.3449")).toBe("$12.34");
    expect(money("-1500.0000")).toBe("−$1,500");
    expect(money("-1500.0000", { cents: true })).toBe("−$1,500.00");
  });

  it("shows no minus sign on a value that rounds to zero", () => {
    expect(money("-0.001")).toBe("$0");
    expect(money("-0.0000")).toBe("$0");
    expect(money("-0.004", { cents: true })).toBe("$0.00");
  });

  it("uses the value's currency, USD when none is given", () => {
    expect(money("12.5", { currency: "EUR" })).toBe("€12.50");
    expect(money("-12.5", { currency: "GBP" })).toBe("−£12.50");
    expect(money("12.5", { currency: "CAD" })).toBe("CA$12.50");
    expect(money("12.5", { currency: "CHF" })).toBe("CHF 12.50");
    expect(money("12.5", { currency: "usd" })).toBe("$12.50");
    expect(money("12.5", { currency: null })).toBe("$12.50");
    expect(money("2500", { currency: "EUR" })).toBe("€2,500");
    // No minor unit: rounded half-up to whole yen, with or without cents.
    expect(money("1234.5", { currency: "JPY" })).toBe("¥1,235");
    expect(money("12.5", { currency: "JPY", cents: true })).toBe("¥13");
    // A malformed code shows as itself rather than failing.
    expect(money("5", { currency: "Q1" })).toBe("Q1 5");
  });

  it("shows the sign as asked", () => {
    expect(money("5", { sign: true })).toBe("+$5");
    expect(money("12.34", { sign: true })).toBe("+$12.34");
    expect(money("-5", { sign: true })).toBe("−$5");
    expect(money("0", { sign: true })).toBe("$0");
    expect(money("0", { sign: true, cents: true })).toBe("$0.00");
  });

  it("reads withheld for a missing value and shows a malformed one as is", () => {
    expect(money(null)).toBe("withheld");
    expect(money(undefined)).toBe("withheld");
    expect(money("")).toBe("—");
    expect(money("1e5")).toBe("1e5");
    expect(money("abc")).toBe("abc");
  });
});

describe("moneyCompact", () => {
  it("shortens axis labels on the digit string", () => {
    expect(moneyCompact("2500")).toBe("$2.5k");
    expect(moneyCompact("450000")).toBe("$450k");
    expect(moneyCompact("1200000")).toBe("$1.2M");
    expect(moneyCompact("-1250000.00")).toBe("−$1.25M");
    expect(moneyCompact("950.49")).toBe("$950");
    expect(moneyCompact("0")).toBe("$0");
    expect(moneyCompact("-0.4")).toBe("$0");
    expect(moneyCompact("1500", "EUR")).toBe("€1.5k");
  });
});

/* Guardrail 1 in the bundles: a widget turns an amount into a number only
 * through num(), for pixel layout. The dev harness (not shipped) is exempt. */
const SOURCES = import.meta.glob<string>(["../*/*.ts", "!../**/*.test.ts", "!../harness/*.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("widget sources", () => {
  it("format, compare and add amounts as strings", () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(5);
    for (const [path, source] of Object.entries(SOURCES)) {
      const floats = source.match(/\bNumber\(|parseFloat\(|\.toFixed\(|\.toLocaleString\(/g) ?? [];
      // kit.ts defines num(), the one Number() call.
      expect(floats.length, path).toBe(/(^|\/)kit\.ts$/.test(path) ? 1 : 0);
    }
  });
});

describe("estimate (#556)", () => {
  it("marks an estimate with U+2248 and a space before money()'s text", () => {
    expect(estimate("-95.00")).toBe("\u2248 \u2212$95");
    expect(estimate("-95.50")).toBe("\u2248 \u2212$95.50");
    expect(estimate("1190", { sign: true })).toBe("\u2248 +$1,190");
    expect(estimate(null)).toBe("withheld"); // a withheld amount is not marked
  });

  it("says estimated, not the mark, to a screen reader", () => {
    const node = estimateNode("-1190.00", { sign: true });
    expect(shownText(node)).toBe("\u2248 \u2212$1,190");
    expect(spokenText(node)).toBe("estimated \u2212$1,190");
    expect(node.querySelector('[aria-hidden="true"]')?.textContent).toBe("\u2248");
    expect(node.querySelector(".visually-hidden")?.textContent).toBe("estimated");
    expect(css).toMatch(/\.visually-hidden\s*\{[^}]*clip: rect\(0, 0, 0, 0\)/);
    expect(estimateNode(null).textContent).toBe("withheld");
  });
});

