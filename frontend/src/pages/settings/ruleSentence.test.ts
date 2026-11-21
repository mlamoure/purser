import { describe, expect, it } from "vitest";
import { accountOut } from "../../test/fixtures";
import { namesFrom } from "../../lib/names";
import { actionWords, predicateWords, ruleSentence, splitClauses } from "./ruleSentence";

const name = namesFrom([
  [
    accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
    accountOut({ id: "a-2", name: "Savings", path: "assets.savings" }),
    accountOut({ id: "a-3", name: "Rewards Card", path: "liabilities.card", type: "credit_card" }),
  ],
  [accountOut({ id: "c-1", name: "Fitness", path: "expenses.fitness", is_virtual: true })],
]);

describe("rules as sentences (#541)", () => {
  it("reads a categorize rule with names", () => {
    expect(
      ruleSentence(
        { predicate: 'account in assets.checking AND payee contains "acme"', action: "-> expenses.fitness" },
        name,
      ),
    ).toBe('On Checking, payee contains "acme" → category Fitness');
    expect(
      ruleSentence(
        { predicate: 'payee contains "acme" AND account in assets.checking, assets.savings', action: "-> expenses.fitness" },
        name,
      ),
    ).toBe('Payee contains "acme" on Checking or Savings → category Fitness');
  });

  it("reads amounts, direction and dates in words", () => {
    expect(
      predicateWords(
        'description prefix "ACH" AND amount >= 10.0000 AND amount <= -2.5000 AND debit AND on or after 2026-01-01',
        name,
      ),
    ).toBe('Description starts with "ACH", amount is at least $10.00, amount is at most -$2.50, money out, from Jan 1, 2026');
    expect(predicateWords('payee_or_description equals "x" AND amount = 60.0000 AND credit', name)).toBe(
      'Payee or description is "x", amount is $60.00, money in',
    );
    expect(predicateWords("every row", name)).toBe("Every transaction");
  });

  it("reads transfer and rename actions", () => {
    expect(actionWords("-> [assets.savings]", name)).toBe("transfer to Savings");
    expect(actionWords('-> payee "Acme Gym"', name)).toBe('rename the payee to "Acme Gym"');
    expect(actionWords("link assets.checking <-> liabilities.card", name)).toBe(
      "link transfers between Checking and Rewards Card",
    );
  });

  it("reads a transfer_link predicate", () => {
    expect(predicateWords('a leg\'s payee is "Card Services" or an alias AND legs <= 3 days apart', name)).toBe(
      'One side\'s payee is "Card Services" (or an alias), sides within 3 days',
    );
    expect(predicateWords("legs <= 0 days apart", name)).toBe("Both sides on the same day");
  });

  it("keeps a quoted AND inside the match text and shows unknown clauses as written", () => {
    expect(splitClauses('payee contains "A AND B" AND debit')).toEqual(['payee contains "A AND B"', "debit"]);
    expect(predicateWords("memo matches something AND debit", name)).toBe("Memo matches something, money out");
    expect(actionWords("-> expenses.retired_thing", name)).toBe("category Retired thing");
    expect(actionWords("merge everything", name)).toBe("merge everything");
  });
});
