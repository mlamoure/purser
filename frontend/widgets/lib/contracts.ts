/* Widget-side mirrors of the structuredContent contracts declared in
 * purser/mcp/toolschemas.py (the server side validates every response
 * against those TypedDicts, so these stay honest by construction).
 * Amounts are decimal strings end-to-end. */

/** An estimated amount's band (#247): low/high in the account's sign. */
export interface ForecastRange {
  low: string;
  high: string;
}

export interface ForecastLow {
  date: string;
  balance: string;
  /** Present when an estimated event has a band (decision 85): the
   * pessimistic low (on low_date) and the optimistic low. */
  range?: ForecastRange & { low_date: string };
}

export interface ForecastEvent {
  date: string;
  name: string;
  amount: string;
  balance_after: string;
  transaction_id: string | null;
  /** Expanded from the rule past the materialized horizon (#235). */
  projected?: boolean;
  /** Dated before today and still due — lands on day 0. */
  overdue?: boolean;
  /** Only for an amount someone supplied (decision 80). */
  basis?: "statement" | "override";
  /** An estimated amount's band (decision 85). */
  range?: ForecastRange;
}

/** What the line is made of (#235): zero active schedules = flat by construction. */
export interface ForecastCoverage {
  schedules_active: number;
  instances: number;
  projected: number;
  suggestions_open: number;
  /** Instances left out as paid: an open match proposal names a bank row
   * already in the balance (decision 76). */
  matches_pending: number;
  /** Their net — present only when nonzero. */
  matches_pending_net?: string;
}

/** Every movement `events` does not list (#354): start_balance + the listed
 * amounts + net = end_balance. */
export interface ForecastOmitted {
  count: number;
  net: string;
}

export interface ForecastResult {
  account: string;
  as_of: string;
  horizon_days: number;
  start_balance: string;
  end_balance: string;
  min_balance: string | null;
  low: ForecastLow;
  coverage?: ForecastCoverage;
  /** Fitted to the response budget; the low's event is always listed. */
  events: ForecastEvent[];
  /** The budget (not materiality) left a movement out. */
  truncated: boolean;
  omitted?: ForecastOmitted;
  daily_uri: string;
  links?: Record<string, string>;
}

export interface ForecastDailyDay {
  date: string;
  balance: string;
  /** The pessimistic / optimistic balances — present only when the forecast
   * has a band (decision 85). */
  low?: string;
  high?: string;
  events: {
    name: string;
    amount: string;
    transaction_id: string | null;
    projected?: boolean;
    overdue?: boolean;
    basis?: string | null;
    range?: ForecastRange | null;
  }[];
}

export interface ForecastDailySeries {
  account: string;
  horizon_days: number;
  start_balance: string;
  end_balance: string;
  low: ForecastLow;
  min_balance: string | null;
  first_shortfall: string | null;
  coverage?: ForecastCoverage;
  days: ForecastDailyDay[];
}

export interface RecurringChange {
  date: string;
  delta: string;
}

export interface RecurringItem {
  schedule_id: string;
  payee: string;
  cadence: string;
  amount: string;
  monthly_equiv: string;
  next_due: string | null;
  source: string;
  last_change: RecurringChange | null;
}

export interface RecurringResult {
  monthly_total: string;
  annualized_total: string;
  count: number;
  changes_12mo: { count: number; delta: string };
  items: RecurringItem[];
  truncated: boolean;
  /** Schedule suggestions awaiting a decision (#233) — never rows here. */
  suggestions_open: number;
}

export interface NetWorthPoint {
  date: string;
  assets: string;
  liabilities: string;
  net: string;
}

export interface NetWorthSeriesResult {
  current: string;
  change: { period: string; delta: string; pct: string | null };
  series: NetWorthPoint[];
  attribution: { bucket: string; delta: string }[];
  series_uri: string;
  links?: Record<string, string>;
}

export interface NetWorthPointResult {
  as_of: string;
  net_worth: string;
  assets: string;
  liabilities: string;
  breakdown: { path: string; value: string | null; method: string }[];
}

export type NetWorthResult = NetWorthSeriesResult | NetWorthPointResult;

export interface SpendingMover {
  payee: string;
  delta: string;
  count: number;
  baseline_count: number;
}

export interface SpendingBaselineRow {
  key: string;
  amount: string;
  baseline: string;
  delta: string;
  top_payee_mover?: SpendingMover | null;
}

export interface SpendingBaselineResult {
  group_by: string;
  period: string;
  baseline: string;
  grand_total: string;
  baseline_total: string;
  rows: SpendingBaselineRow[];
  truncated: boolean;
  links?: Record<string, string>;
}

export interface SpendingClassicResult {
  group_by: string;
  filter: { start: string | null; end: string | null; category: string | null };
  grand_total: string | null;
  truncated: boolean;
  rows: { key: string; total: string | null; transactions: number }[];
}

export type SpendingResult = SpendingBaselineResult | SpendingClassicResult;

export interface SearchResult {
  total: number;
  returned: number;
  items: { id: string; date: string; amount: string | null; payee: string | null }[];
}

export interface TransactionDetail {
  id: string;
  date: string;
  status: string;
  payee: string | null;
  description: string | null;
  postings: { account: string; amount: string | null; currency: string }[];
}

/* balance_history (#199) — parallel arrays, not point objects: at 31 points
 * the repeated keys would push structuredContent past the token budget. */
export interface BalanceHistorySeries {
  dates: string[];
  balances: string[];
}

export interface BalanceHistoryExtreme {
  date: string;
  balance: string;
}

export interface BalanceHistoryMover {
  date: string;
  payee: string | null;
  amount: string;
  transaction_id: string;
}

export interface BalanceHistoryResult {
  account: string;
  start: string;
  end: string;
  currency: string;
  method: string;
  current: string;
  change: { period: string; delta: string; pct: string | null };
  low: BalanceHistoryExtreme | null;
  high: BalanceHistoryExtreme | null;
  density: { interval_days: number; points: number };
  transactions: number;
  series: BalanceHistorySeries;
  movers: BalanceHistoryMover[];
  series_uri: string;
}

export interface BalanceHistoryFullSeries {
  account: string;
  start: string;
  end: string;
  currency: string;
  method: string;
  opening: string;
  points: { date: string; balance: string; delta: string; transaction_id: string }[];
  truncated: boolean;
}
