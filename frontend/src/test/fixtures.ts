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
    "Link transfers between assets.checking and assets.savings when the payee is " +
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
    "on assets.checking — variance 85.00",
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
    "Link 250.00 from assets.checking (2026-10-14) to liabilities.card (2026-10-15) as one " +
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
    privacy_purchase: false,
    privacy_amount: false,
    ai_provider: "none",
    ai_model: null,
    ai_base_url: "https://api.example.com/v1",
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
