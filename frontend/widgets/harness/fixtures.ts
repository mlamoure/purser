/* Synthetic fixtures the harness serves to widgets — the same numbers as the
 * design mockups. Decimal strings only; nothing real (guardrail 1/7). */

export interface WidgetFixture {
  /** The tool arguments the "agent" used (sent as ui/notifications/tool-input). */
  toolInput: Record<string, unknown>;
  /** The structuredContent delivered as the initial tool result. */
  result: unknown;
  /** Canned tools/call replies, keyed by tool name. */
  tools: Record<string, (args: Record<string, unknown>) => unknown>;
  /** Canned resources/read replies, keyed by URI. */
  resources: Record<string, unknown>;
}

function forecastResult(days: number): Record<string, unknown> {
  const events = [
    { date: "2026-07-14", name: "Mortgage", amount: "-2400.00", balance_after: "1048.00", transaction_id: "11111111-1111-4111-8111-111111111111" },
    { date: "2026-07-19", name: "Car insurance", amount: "-1190.00", balance_after: "-142.00", transaction_id: "22222222-2222-4222-8222-222222222222", range: { low: "-1290.00", high: "-1100.00" } },
    { date: "2026-07-20", name: "Payroll", amount: "3100.00", balance_after: "2958.00", transaction_id: "33333333-3333-4333-8333-333333333333" },
    { date: "2026-08-01", name: "Rent", amount: "-1800.00", balance_after: "1158.00", transaction_id: "44444444-4444-4444-8444-444444444444", range: { low: "-1900.00", high: "-1750.00" } },
    { date: "2026-08-03", name: "Payroll", amount: "3100.00", balance_after: "4258.00", transaction_id: "55555555-5555-4555-8555-555555555555" },
  ];
  const horizonEnd = new Date(Date.parse("2026-07-08") + days * 86_400_000).toISOString().slice(0, 10);
  const listed = events.filter((e) => e.date <= horizonEnd);
  return {
    account: "assets.checking",
    as_of: "2026-07-08",
    horizon_days: days,
    start_balance: "3448.00",
    end_balance: listed.length ? listed[listed.length - 1].balance_after : "3448.00",
    min_balance: "500.00",
    low: {
      date: "2026-07-19",
      balance: "-142.00",
      ...(listed.some((e) => "range" in e) ? { range: { low: "-242.00", high: "-52.00", low_date: "2026-07-19" } } : {}),
    },
    events: listed,
    coverage: {
      schedules_active: 4,
      instances: 5,
      projected: 0,
      suggestions_open: 0,
      matches_pending: 1,
      matches_pending_net: "-200.00",
    },
    truncated: false,
    daily_uri: `purser://forecast/assets.checking/${days}d`,
  };
}

/* The pessimistic / optimistic balance after each event (decision 85): every
 * ranged event at its low / its high, worked out by hand from the events. */
const BAND_AFTER: Record<string, [string, string]> = {
  "2026-07-14": ["1048.00", "1048.00"],
  "2026-07-19": ["-242.00", "-52.00"],
  "2026-07-20": ["2858.00", "3048.00"],
  "2026-08-01": ["958.00", "1298.00"],
  "2026-08-03": ["4058.00", "4398.00"],
};

function forecastDaily(days: number): Record<string, unknown> {
  const result = forecastResult(days) as {
    end_balance: string;
    low: { date: string; balance: string };
    events: { date: string; balance_after: string; range?: unknown }[];
  };
  const banded = result.events.some((e) => e.range !== undefined);
  const out: { date: string; balance: string; low?: string; high?: string; events: unknown[] }[] = [];
  let balance = "3448.00";
  let band: [string, string] = ["3448.00", "3448.00"];
  for (let offset = 0; offset <= days; offset += 1) {
    const date = new Date(Date.parse("2026-07-08") + offset * 86_400_000).toISOString().slice(0, 10);
    const hit = result.events.find((e) => e.date === date);
    if (hit) {
      balance = hit.balance_after;
      band = BAND_AFTER[date];
    }
    out.push({ date, balance, ...(banded ? { low: band[0], high: band[1] } : {}), events: [] });
  }
  return {
    account: "assets.checking",
    horizon_days: days,
    start_balance: "3448.00",
    end_balance: result.end_balance,
    low: result.low,
    min_balance: "500.00",
    first_shortfall: days >= 11 ? "2026-07-19" : null,
    days: out,
  };
}

const transactionDetail = {
  id: "22222222-2222-4222-8222-222222222222",
  date: "2026-07-19",
  status: "expected",
  origin: "rule",
  payee: "GEICO",
  description: "Car insurance (expected)",
  postings: [
    { account: "assets.checking", amount: "-1190.00", currency: "USD" },
    { account: "expenses.insurance.auto", amount: "1190.00", currency: "USD" },
  ],
  provenance: { content_hash: null, schedule_id: "66666666-6666-4666-8666-666666666666", source_fetch_id: null, superseded_by: null, reverses_id: null },
};

const recurringResult = {
  monthly_total: "412.37",
  annualized_total: "4948.44",
  count: 14,
  changes_12mo: { count: 3, delta: "7.50" },
  items: [
    { schedule_id: "77777777-7777-4777-8777-777777777777", payee: "Verizon Wireless", cadence: "monthly", amount: "89.40", monthly_equiv: "89.40", next_due: "2026-07-22", source: "manual", last_change: null },
    { schedule_id: "88888888-8888-4888-8888-888888888888", payee: "GEICO auto", cadence: "every 6 months", amount: "920.00", monthly_equiv: "153.33", next_due: "2026-07-19", source: "detected", last_change: null },
    { schedule_id: "99999999-9999-4999-8999-999999999999", payee: "Netflix", cadence: "monthly", amount: "17.99", monthly_equiv: "17.99", next_due: "2026-07-12", source: "manual", last_change: { date: "2026-03-14", delta: "2.50" } },
  ],
  truncated: false,
  suggestions_open: 2,
};


/* balance_history (#199): a 31-point daily 30d window with one big outflow
 * and one payroll, so the mover dots and the low label both have something
 * to point at. */
function balanceHistoryResult(period: string): Record<string, unknown> {
  const spans: Record<string, number> = { "30d": 30, "90d": 90, "1y": 365, all: 500 };
  const days = spans[period] ?? 30;
  const step = days <= 30 ? 1 : days <= 90 ? 3 : 14;
  const dates: string[] = [];
  const balances: string[] = [];
  let balance = 4060.4;
  for (let d = days; d >= 0; d -= step) {
    const iso = new Date(Date.UTC(2026, 7, 9) - d * 86400000).toISOString().slice(0, 10);
    if (iso === "2026-07-19") balance -= 2400;
    if (iso === "2026-07-26") balance += 2148;
    balance -= 12.4;
    dates.push(iso);
    balances.push(balance.toFixed(2));
  }
  return {
    account: "assets.checking",
    start: dates[0],
    end: dates[dates.length - 1],
    currency: "USD",
    method: "postings",
    current: balances[balances.length - 1],
    change: {
      period,
      delta: (Number(balances[balances.length - 1]) - Number(balances[0])).toFixed(2),
      pct: "-15.1",
    },
    low: { date: "2026-07-19", balance: "1842.10" },
    high: { date: dates[0], balance: balances[0] },
    density: { interval_days: step, points: dates.length },
    transactions: 12,
    series: { dates, balances },
    movers: [
      {
        date: "2026-07-19",
        payee: "Bank Mortgage",
        amount: "-2400.00",
        transaction_id: "bh-1",
      },
      { date: "2026-07-26", payee: "Employer Inc", amount: "2148.00", transaction_id: "bh-2" },
    ],
    series_uri: `purser://balance-history/assets.checking/${period}`,
  };
}

function balanceHistoryFull(period: string): Record<string, unknown> {
  const sampled = balanceHistoryResult(period) as {
    start: string;
    end: string;
    series: { dates: string[]; balances: string[] };
  };
  return {
    account: "assets.checking",
    start: sampled.start,
    end: sampled.end,
    currency: "USD",
    method: "postings",
    opening: "4072.80",
    points: sampled.series.dates.map((date, i) => ({
      date,
      balance: sampled.series.balances[i],
      delta: "-12.40",
      transaction_id: `bh-full-${i}`,
    })),
    truncated: false,
  };
}

const forecastSeries: Record<string, unknown> = {
  "purser://forecast/assets.checking/30d": forecastDaily(30),
  "purser://forecast/assets.checking/60d": forecastDaily(60),
  "purser://forecast/assets.checking/90d": forecastDaily(90),
};

const balanceHistorySeries: Record<string, unknown> = {
  "purser://balance-history/assets.checking/30d": balanceHistoryFull("30d"),
  "purser://balance-history/assets.checking/90d": balanceHistoryFull("90d"),
  "purser://balance-history/assets.checking/1y": balanceHistoryFull("1y"),
  "purser://balance-history/assets.checking/all": balanceHistoryFull("all"),
};

/** read_series (#354) answers with the resource body itself — the harness
 * wraps a reply as JSON text, which is exactly the body's serialization —
 * so a widget can be exercised as behind a gateway that forwards tools only. */
function readSeries(series: Record<string, unknown>): (args: Record<string, unknown>) => unknown {
  return (args) => series[String(args.uri)];
}

export const FIXTURES: Record<string, WidgetFixture> = {
  recurring: {
    toolInput: {},
    result: recurringResult,
    tools: {
      list_recurring: () => recurringResult,
      search_transactions: () => ({
        total: 3,
        returned: 3,
        items: [
          { id: "t-1", date: "2026-06-12", amount: "-17.99", payee: "Netflix" },
          { id: "t-2", date: "2026-05-12", amount: "-17.99", payee: "Netflix" },
          { id: "t-3", date: "2026-04-12", amount: "-15.49", payee: "Netflix" },
        ],
      }),
    },
    resources: {},
  },
  forecast: {
    toolInput: { account: "assets.checking", days: 30 },
    result: forecastResult(30),
    tools: {
      forecast_balance: (args) => forecastResult(Number(args.days ?? 30)),
      get_transaction: () => transactionDetail,
      read_series: readSeries(forecastSeries),
    },
    resources: forecastSeries,
  },
  "balance-history": {
    toolInput: { account: "assets.checking", period: "30d" },
    result: balanceHistoryResult("30d"),
    tools: {
      balance_history: (args) => balanceHistoryResult(String(args.period ?? "30d")),
      forecast_balance: (args) => forecastResult(Number(args.days ?? 30)),
      get_transaction: () => transactionDetail,
      read_series: readSeries({ ...balanceHistorySeries, ...forecastSeries }),
    },
    resources: balanceHistorySeries,
  },
};
