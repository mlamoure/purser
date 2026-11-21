/* Words for the values the API stores (issue #541).
 *
 * The API speaks in storage terms: account types ("credit_card"), rule kinds
 * ("transfer_link"), amount bases ("counterparty_balance") and ltree paths
 * ("expenses.dining.coffee"). People read names. Every page turns a stored
 * value into words through this one module, so a value reads the same
 * everywhere and a new one degrades to a readable fallback ("some_value" ->
 * "Some value") rather than to the raw string.
 *
 * Only the display changes: what a page SENDS to the API stays the stored
 * value. */

/** "credit_card" -> "Credit card"; "expenses" -> "Expenses". */
export function humanize(value: string): string {
  const words = value.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return words ? capitalize(words) : value;
}

/** The text with its first letter upper-cased, the rest untouched. */
export const capitalize = (text: string) => (text ? text[0].toUpperCase() + text.slice(1) : text);

/** The label `map` gives `value`, else its humanized form. null/"" -> "—". */
export function labelFor(map: Record<string, string>, value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  return map[value] ?? humanize(value);
}

/** Account types (DESIGN §3.1). */
export const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  checking: "Checking",
  savings: "Savings",
  cash: "Cash",
  credit_card: "Credit card",
  brokerage: "Brokerage",
  property: "Asset",
  loan: "Loan",
  category_expense: "Expense category",
  category_income: "Income category",
  equity: "Equity",
};

export const accountTypeLabel = (type: string | null | undefined) =>
  labelFor(ACCOUNT_TYPE_LABELS, type);

/** Rule kinds (DESIGN §3.1 rules.type). */
export const RULE_TYPE_LABELS: Record<string, string> = {
  categorize: "Categorize",
  rename: "Rename payee",
  transfer_link: "Link transfers",
};

/** Where a scheduled amount comes from (decision 80): the estimate bases a
 * schedule stores, plus the instance-only "override" and "statement". */
export const AMOUNT_BASIS_LABELS: Record<string, string> = {
  fixed: "fixed",
  last: "last amount",
  rolling_avg_3: "3-payment average",
  rolling_avg_12: "12-payment average",
  counterparty_balance: "current balance",
  override: "your amount",
  statement: "statement balance",
};

export const amountBasisLabel = (basis: string | null | undefined) =>
  basis ? (AMOUNT_BASIS_LABELS[basis] ?? humanize(basis).toLowerCase()) : "—";

/** When a schedule's instance posts (schedules.autopost_policy). */
export const AUTOPOST_LABELS: Record<string, string> = {
  wait_for_match: "Wait for the bank row",
  auto_post: "Post automatically",
  reminder_only: "Reminder only",
};

/** The last label of an ltree path, as words: the fallback name for a path
 * no list knows yet ("expenses.dining.coffee_shops" -> "Coffee shops"). */
export function pathLeafLabel(path: string): string {
  const leaf = path.split(".").filter(Boolean).pop() ?? path;
  return humanize(leaf);
}
