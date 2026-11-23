/* Typed builders for API payloads the component tests mock. Synthetic data
 * only; money stays in decimal strings, as the API sends it. */

import type { components } from "../api/schema";

type Schemas = components["schemas"];

export const accountOut = (over: Partial<Schemas["AccountOut"]> = {}): Schemas["AccountOut"] => ({
  id: "a-1",
  name: "Checking",
  path: "assets.checking",
  type: "checking",
  currency: "USD",
  description: null,
  external_id: null,
  connection_id: null,
  parent_id: null,
  is_virtual: false,
  feed_invert: false,
  holdings_source: "feed",
  feed_balance_is_cash: false,
  balance_true_up: false,
  min_balance: null,
  closed_at: null,
  hidden_at: null,
  created_at: "2026-01-05T12:00:00Z",
  secured_loan_ids: [],
  ...over,
});

export const counts = (over: Partial<Schemas["ImportCounts"]> = {}): Schemas["ImportCounts"] => ({
  in_file: 0,
  would_import: 0,
  transfers: 0,
  one_leg_transfers: 0,
  opening_balances: 0,
  promoted: 0,
  uncleared: 0,
  skipped_dedup: 0,
  skipped_cutoff: 0,
  skipped_unmapped: 0,
  skipped_zero_amount: 0,
  skipped_scheduled: 0,
  skipped_investment: 0,
  split_transfers: 0,
  split_remainders: 0,
  ...over,
});

/** One file account in a preview; unmapped, three rows, signs unremarkable. */
export const previewAccount = (
  over: Partial<Schemas["ImportPreviewAccount"]> = {},
): Schemas["ImportPreviewAccount"] => ({
  name: "Everyday Checking",
  type: "checking",
  transactions: 3,
  default_cutoff: null,
  mapped: null,
  sign: { positive: "0.00", negative: "-37.02", net: "-37.02", suspect: false },
  counts: counts({ in_file: 3, skipped_unmapped: 3 }),
  ...over,
});

export const previewResponse = (
  accounts: Schemas["ImportPreviewAccount"][],
): Schemas["ImportPreviewResponse"] => ({
  accounts,
  categories: [],
  date_range: { start: "2026-01-02", end: "2026-02-27" },
  total_transactions: accounts.reduce((n, a) => n + a.transactions, 0),
});

export const importResult = (over: Partial<Schemas["ImportResult"]> = {}): Schemas["ImportResult"] => ({
  imported: 0,
  transfers_created: 0,
  transfers_absorbed: 0,
  transfers_auto_linked: 0,
  one_leg_transfers: 0,
  opening_balances: 0,
  promoted: 0,
  uncleared: 0,
  skipped: 0,
  skipped_cutoff: 0,
  skipped_unmapped: 0,
  skipped_zero_amount: 0,
  skipped_scheduled: 0,
  skipped_investment: 0,
  split_transfers: 0,
  split_remainders: 0,
  categories_created: [],
  per_account: {},
  ...over,
});

/** A transfer_link suggestion a manual confirm proposed (#271): the pair,
 * the payee scope, nothing narrowed yet, one confirmed pair as evidence. */
export const transferLinkSuggestion = (
  over: Partial<Schemas["SuggestionOut"]> = {},
): Schemas["SuggestionOut"] => ({
  id: "s-1",
  kind: "transfer_link",
  status: "open",
  source: "confirm",
  confidence: null,
  headline:
    "Link transfers between Checking and Rainy Day Savings when the payee is " +
    "ZEPHYR BKG SVC SENT (or an alias)",
  draft: {
    version: 1,
    from_account: "assets.checking",
    to_account: "assets.savings",
    payee: "ZEPHYR BKG SVC SENT",
    payee_id: "p-1",
    amount_exact: null,
    window_days: null,
    effective_from: null,
  },
  evidence_summary: {
    count: 1,
    first: "2026-05-04",
    last: "2026-05-04",
    amount_range: { low: "250.00", high: "250.00" },
    cadence: null,
  },
  snooze_until: null,
  seen_count: 1,
  created_at: "2026-05-06T12:00:00Z",
  decided_at: null,
  reject_scope: null,
  result_entity_type: null,
  result_entity_id: null,
  ...over,
});

/** A match suggestion (#246, decision 86): an open proposal pairing a bill
 * with a bank row that paid less than expected. */
export const matchSuggestion = (
  over: Partial<Schemas["SuggestionOut"]> = {},
): Schemas["SuggestionOut"] => ({
  id: "s-3",
  kind: "match",
  status: "open",
  source: "detected",
  confidence: "0.612",
  headline:
    "Match City Utilities (-180.00 due 2026-07-15) to the -95.00 bank row of 2026-07-18 " +
    "on Checking — variance 85.00",
  draft: {
    version: 1,
    expected_id: "e-1",
    posted_id: "t-1",
    account: "assets.checking",
    label: "City Utilities",
    expected_date: "2026-07-15",
    expected_amount: "-180.00",
    posted_date: "2026-07-18",
    posted_amount: "-95.00",
    variance: "85.00",
  },
  evidence_summary: {
    count: 1,
    first: "2026-07-18",
    last: "2026-07-18",
    amount_range: { low: "-95.00", high: "-95.00" },
    cadence: null,
  },
  snooze_until: null,
  seen_count: 1,
  created_at: "2026-07-18T07:00:00Z",
  decided_at: null,
  reject_scope: null,
  result_entity_type: null,
  result_entity_id: null,
  ...over,
});

export const ruleOut = (over: Partial<Schemas["RuleOut"]> = {}): Schemas["RuleOut"] => ({
  id: "r-1",
  type: "transfer_link",
  state: "draft",
  priority: 100,
  predicate: 'a leg\'s payee is "ZEPHYR BKG SVC SENT" or an alias AND legs <= 3 days apart',
  action: "link assets.checking <-> assets.savings",
  applied_count: 0,
  note: null,
  window_days: 3,
  payee_scope: "ZEPHYR BKG SVC SENT",
  securities_trade: false,
  ...over,
});

/** A transfer_pair suggestion (#386, decision 102): 250.00 leaves checking and
 * arrives on the card a day later, with nothing in the bank text to link it. */
export const transferPairSuggestion = (
  over: Partial<Schemas["SuggestionOut"]> = {},
): Schemas["SuggestionOut"] => ({
  id: "s-4",
  kind: "transfer_pair",
  status: "open",
  source: "detected",
  confidence: null,
  headline:
    "Link 250.00 from Checking (2026-10-14) to Example Card (2026-10-15) as one " +
    "transfer",
  draft: {
    version: 1,
    outgoing_id: "t-out",
    incoming_id: "t-in",
    from_account: "assets.checking",
    to_account: "liabilities.card",
    amount: "250.00",
    outgoing_date: "2026-10-14",
    incoming_date: "2026-10-15",
    date_gap_days: 1,
    outgoing_description: "EXAMPLE BANK WEB DEBIT",
    incoming_description: "THANK YOU FOR YOUR REMITTANCE",
  },
  evidence_summary: {
    count: 1,
    first: "2026-10-14",
    last: "2026-10-14",
    amount_range: { low: "250.00", high: "250.00" },
    cadence: null,
  },
  snooze_until: null,
  seen_count: 1,
  created_at: "2026-10-15T07:00:00Z",
  decided_at: null,
  reject_scope: null,
  result_entity_type: null,
  result_entity_id: null,
  ...over,
});

/** A transfer candidate as the register lists it; `suggestion_id` names its
 * inbox card when it has one. */
export const transferCandidate = (
  over: Partial<Schemas["TransferCandidateOut"]> = {},
): Schemas["TransferCandidateOut"] => ({
  outgoing_id: "t-out",
  incoming_id: "t-in",
  amount: "250.0000",
  date_gap_days: 1,
  outgoing_description: "EXAMPLE BANK WEB DEBIT",
  incoming_description: "THANK YOU FOR YOUR REMITTANCE",
  suggestion_id: null,
  ...over,
});

/** GET /config as the server answers it: every value at its default, key and
 * secrets unset, encryption on with the key not yet recorded. */
export const configOut = (
  values: Partial<Schemas["ConfigOut"]["values"]> = {},
  over: Partial<Omit<Schemas["ConfigOut"], "values">> = {},
): Schemas["ConfigOut"] => ({
  values: {
    sync_schedule: { preset: "daily", hour: 6, minute: 30 },
    transfer_auto_link: true,
    ai_provider: "none",
    ai_model: null,
    ai_base_url: "https://api.example.com/v1",
    ai_timeout_seconds: 30,
    grafana_url: null,
    session_ttl_days: 7,
    session_idle_hours: 8,
    audit_retention_days: 90,
    oidc_issuer: null,
    oidc_client_id: null,
    oidc_enabled: false,
    oidc_auto_provision: false,
    price_provider: "none",
    price_stale_days: 7,
    feed_stale_days: 4,
    timezone: "UTC",
    ...values,
  },
  overridden: [],
  ai_api_key_set: false,
  ai_disclosure: [
    {
      capability: "categorize",
      when: "To categorize a transaction that no rule or earlier choice covers",
      sends: ["its payee, description and amount", "your categories and their descriptions"],
    },
    {
      capability: "review",
      when: "With the review pass on, to review a transaction",
      sends: ["its date and amount", "your reviewer checklist"],
    },
  ],
  oidc_client_secret_set: false,
  price_api_key_set: false,
  sync_reschedule_live: true,
  encryption: {
    field_encryption_active: true,
    canary_ok: true,
    key_fingerprint: "abcd1234",
    key_acknowledged_at: null,
    encrypted_scopes: [],
  },
  ...over,
});

/** A split suggestion (#590, decision 118): a new 2550.00 paycheck offered
 * the payee's last split, the 50.00 difference on the salary line and the
 * 401(k) line backed by the one exact bank row. */
export const splitSuggestion = (
  over: Partial<Schemas["SuggestionOut"]> = {},
): Schemas["SuggestionOut"] => ({
  id: "s-5",
  kind: "split",
  status: "open",
  source: "detected",
  confidence: null,
  headline: "Split the 2550.00 row of 2026-11-14 on Checking like last time (3 lines)",
  draft: {
    version: 1,
    transaction_id: "t-pay",
    template_id: "t-last",
    account: "assets.checking",
    date: "2026-11-14",
    amount: "2550.00",
    template_amount: "2500.00",
    payee: "EXAMPLE PAYROLL",
    lines: [
      { target: "income.salary", amount: "4050.00", memo: null, backing_ids: [] },
      { target: "expenses.taxes", amount: "-900.00", memo: "withholding", backing_ids: [] },
      { target: "[assets.retirement]", amount: "-600.00", memo: null, backing_ids: ["t-401k"] },
    ],
  },
  evidence_summary: {
    count: 1,
    first: "2026-10-31",
    last: "2026-10-31",
    amount_range: { low: "2500.00", high: "2500.00" },
    cadence: null,
  },
  snooze_until: null,
  seen_count: 1,
  created_at: "2026-11-14T07:00:00Z",
  decided_at: null,
  reject_scope: null,
  result_entity_type: null,
  result_entity_id: null,
  ...over,
});

/** A balance-history window (#594): three days, up then down. */
export const balanceHistoryOut = (
  over: Partial<Schemas["BalanceHistoryOut"]> = {},
): Schemas["BalanceHistoryOut"] => ({
  account_id: "a-1",
  path: "assets.checking",
  currency: "USD",
  range: "1y",
  start: "2026-11-01",
  end: "2026-11-03",
  method: "postings",
  interval_days: 1,
  points: [
    { date: "2026-11-01", balance: "100.0000" },
    { date: "2026-11-02", balance: "250.0000" },
    { date: "2026-11-03", balance: "200.0000" },
  ],
  current: "200.0000",
  change: "100.0000",
  change_pct: "100.00",
  low: { date: "2026-11-01", balance: "100.0000" },
  high: { date: "2026-11-02", balance: "250.0000" },
  first_activity: "2026-11-01",
  ...over,
});

/** A brokerage account's holdings table (#595): two lines and some cash. */
export const holdingsSummaryOut = (
  over: Partial<Schemas["HoldingsSummaryOut"]> = {},
): Schemas["HoldingsSummaryOut"] => ({
  account_id: "a-1",
  as_of: "2026-11-20",
  source: "feed",
  closed: false,
  cash: "500.0000",
  cash_weight_pct: "20.83",
  market_value: "1900.0000",
  cost_basis: "1500.0000",
  unrealized_gain: "400.0000",
  gain_pct: "26.67",
  total: "2400.0000",
  lines: [
    holdingLine({ symbol: "AAAA", name: "Example Growth Fund", quantity: "10.000000", price: "150.000000", market_value: "1500.0000", cost_basis: "1000.0000", unrealized_gain: "500.0000", gain_pct: "50.00", weight_pct: "62.50" }),
    holdingLine({ symbol: "BBBB", quantity: "4.000000", price: "100.000000", market_value: "400.0000", cost_basis: "500.0000", unrealized_gain: "-100.0000", gain_pct: "-20.00", weight_pct: "16.67" }),
  ],
  ...over,
});

export const holdingLine = (
  over: Partial<Schemas["HoldingsSummaryLineOut"]> = {},
): Schemas["HoldingsSummaryLineOut"] => ({
  symbol: "AAAA",
  name: null,
  source: "feed",
  position_id: null,
  quantity: "1.000000",
  price: "1.000000",
  price_as_of: "2026-11-20",
  price_source: null,
  market_value: "1.0000",
  cost_basis: null,
  unrealized_gain: null,
  gain_pct: null,
  weight_pct: null,
  in_transit: null,
  stale_price: false,
  no_price: false,
  unpriceable: false,
  value_out_of_range: false,
  ...over,
});
