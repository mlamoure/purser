/* Recurring radar widget (issue #74): boots the host bridge and the demo
 * fixture; the rows and the metric strip are drawn by ./view.ts (#347). */

import type { RecurringResult } from "../lib/contracts";
import { initWidget } from "../lib/host";
import "../lib/theme.css";
import { render } from "./view";

const DEMO: RecurringResult = {
  monthly_total: "412.37",
  annualized_total: "4948.44",
  count: 14,
  changes_12mo: { count: 3, delta: "7.50" },
  items: [
    { schedule_id: "s-1", payee: "Verizon Wireless", cadence: "monthly", amount: "89.40", monthly_equiv: "89.40", next_due: "2026-07-22", source: "manual", last_change: null },
    { schedule_id: "s-5", payee: "GEICO auto", cadence: "every 6 months", amount: "920.00", monthly_equiv: "153.33", next_due: "2026-07-19", source: "detected", last_change: null },
    { schedule_id: "s-2", payee: "Netflix", cadence: "monthly", amount: "17.99", monthly_equiv: "17.99", next_due: "2026-07-12", source: "manual", last_change: { date: "2026-03-14", delta: "2.50" } },
    { schedule_id: "s-3", payee: "YouTube Premium", cadence: "monthly", amount: "13.99", monthly_equiv: "13.99", next_due: "2026-07-18", source: "manual", last_change: { date: "2026-05-02", delta: "2.00" } },
    { schedule_id: "s-4", payee: "Spotify", cadence: "monthly", amount: "11.99", monthly_equiv: "11.99", next_due: "2026-07-25", source: "manual", last_change: null },
  ],
  truncated: false,
  suggestions_open: 2,
};

initWidget<RecurringResult>({ name: "recurring", demo: DEMO, render });
