/* Spending widget (issue #76): bar rows against the user's own baseline —
 * bar = current amount, tick = baseline, delta chips red/green, biggest-
 * mover footnote with payee attribution. Bar click drills the ltree subtree
 * through the same tool; the month stepper walks adjacent periods. Classic
 * (non-baseline) payloads render as plain bars. */

import type { SpendingBaselineResult, SpendingClassicResult, SpendingResult } from "../lib/contracts";
import { initWidget, type WidgetHost } from "../lib/host";
import { CHART, clear, h, icon, money, num } from "../lib/kit";
import "../lib/theme.css";

const DEMO: SpendingBaselineResult = {
  group_by: "category",
  period: "2026-07",
  baseline: "avg_3mo",
  grand_total: "2232.00",
  baseline_total: "1984.00",
  rows: [
    { key: "expenses.dining", amount: "642.00", baseline: "402.00", delta: "240.00", top_payee_mover: { payee: "DoorDash", delta: "198.00", count: 6, baseline_count: 2 } },
    { key: "expenses.groceries", amount: "580.00", baseline: "615.00", delta: "-35.00", top_payee_mover: null },
    { key: "expenses.subscriptions", amount: "412.00", baseline: "407.00", delta: "5.00", top_payee_mover: null },
    { key: "expenses.shopping", amount: "388.00", baseline: "292.00", delta: "96.00", top_payee_mover: { payee: "Amazon", delta: "88.00", count: 5, baseline_count: 3 } },
    { key: "expenses.transport", amount: "210.00", baseline: "198.00", delta: "12.00", top_payee_mover: null },
  ],
  truncated: false,
};

function isBaseline(data: SpendingResult): data is SpendingBaselineResult {
  return "baseline_total" in data;
}

function label(key: string): string {
  const leaf = key.split(".").at(-1) ?? key;
  return leaf.charAt(0).toUpperCase() + leaf.slice(1).replace(/_/g, " ");
}

function monthLabel(period: string): string {
  const [y, m] = period.split("-").map((v) => parseInt(v, 10));
  return `${["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][(m ?? 1) - 1]} ${y}`;
}

function stepMonth(period: string, delta: number): string {
  const [y, m] = period.split("-").map((v) => parseInt(v, 10));
  const total = y * 12 + (m - 1) + delta;
  return `${Math.floor(total / 12)}-${String((total % 12) + 1).padStart(2, "0")}`;
}

function renderBaseline(data: SpendingBaselineResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);

  const delta = num(data.grand_total) - num(data.baseline_total);
  const over = delta >= 0;
  const maxAmount = Math.max(...data.rows.map((r) => Math.max(num(r.amount), num(r.baseline))), 1);

  const drill = data.group_by === "payee";
  const header = h(
    "div",
    { style: "display:flex;align-items:baseline;justify-content:space-between;margin-bottom:16px;" },
    h(
      "div",
      {},
      h(
        "p",
        { style: "font-size:13px;color:var(--text-secondary);margin:0;" },
        `${monthLabel(data.period)} spending${drill ? " — by payee" : ""}`,
      ),
      h(
        "p",
        { style: "font-size:24px;font-weight:500;margin:0;" },
        money(data.grand_total),
        " ",
        h(
          "span",
          { style: `font-size:13px;font-weight:400;color:var(--text-${over ? "danger" : "success"});` },
          `${over ? "+" : "−"}${money(String(Math.abs(delta)))} vs ${data.baseline.replace(/_/g, " ").replace("avg", "").trim() || "baseline"} avg`,
        ),
      ),
    ),
    h(
      "div",
      { style: "display:flex;gap:4px;align-items:center;" },
      h("button", { class: "pill", onclick: () => void recall(stepMonth(data.period, -1)) }, "‹"),
      h("span", { style: "font-size:12px;color:var(--text-muted);" }, "tick = baseline"),
      h("button", { class: "pill", onclick: () => void recall(stepMonth(data.period, 1)) }, "›"),
    ),
  );

  async function recall(period: string, category?: string, groupBy?: string): Promise<void> {
    const next = await host.callTool<SpendingResult>("spending_summary", {
      period,
      baseline: data.baseline,
      ...(category ? { category } : {}),
      ...(groupBy ? { group_by: groupBy } : {}),
    });
    if (next && isBaseline(next)) {
      renderBaseline(next, host);
      host.tellModel(`Viewing ${period} spending vs ${data.baseline}: ${money(next.grand_total)}.`);
    }
  }

  const rows = data.rows.map((row) => {
    const width = (num(row.amount) / maxAmount) * 100;
    const tick = (num(row.baseline) / maxAmount) * 100;
    const rowDelta = num(row.delta);
    return h(
      "div",
      { style: "margin-bottom:14px;" },
      h(
        "div",
        { style: "display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;" },
        h("span", { style: "font-weight:500;" }, label(row.key)),
        h(
          "span",
          { style: "color:var(--text-secondary);" },
          money(row.amount),
          " ",
          h(
            "span",
            { style: `color:var(--text-${rowDelta > 0 ? "danger" : "success"});` },
            rowDelta === 0 ? "" : `${rowDelta > 0 ? "+" : "−"}${money(String(Math.abs(rowDelta)))}`,
          ),
        ),
      ),
      h(
        "div",
        {
          style:
            "position:relative;height:10px;background:var(--surface-1);border-radius:5px;" +
            (drill ? "" : "cursor:pointer;"),
          onclick: () => {
            if (!drill) void recall(data.period, row.key, "payee");
          },
        },
        h("div", {
          style: `position:absolute;left:0;top:0;height:10px;border-radius:5px;background:${CHART.bar};width:${Math.max(2, width)}%;`,
        }),
        h("div", {
          style: `position:absolute;top:-3px;height:16px;width:2px;background:var(--text-muted);left:${Math.min(99, tick)}%;`,
        }),
      ),
    );
  });

  const card = h("div", { class: "card" }, header, ...rows);

  const moverRow = [...data.rows]
    .filter((r) => r.top_payee_mover)
    .sort((a, b) => Math.abs(num(b.delta)) - Math.abs(num(a.delta)))[0];
  if (moverRow?.top_payee_mover) {
    const mover = moverRow.top_payee_mover;
    card.append(
      h(
        "p",
        { style: "font-size:13px;color:var(--text-secondary);margin:14px 0 0;display:flex;align-items:center;gap:6px;" },
        icon("trend-up", "currentColor", 15),
        `Biggest mover: ${label(moverRow.key).toLowerCase()} ${num(moverRow.delta) >= 0 ? "+" : "−"}${money(String(Math.abs(num(moverRow.delta))))}, mostly ${mover.payee} (${mover.count} orders vs your usual ${mover.baseline_count})`,
      ),
    );
  }
  if (data.truncated) {
    card.append(
      h("p", { style: "font-size:12px;color:var(--text-muted);margin:8px 0 0;" }, "Showing the largest categories — totals cover everything."),
    );
  }
  root.append(card);
}

function renderClassic(data: SpendingClassicResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);
  const maxTotal = Math.max(...data.rows.map((r) => Math.abs(num(r.total ?? "0"))), 1);
  root.append(
    h(
      "div",
      { class: "card" },
      h(
        "p",
        { style: "font-size:13px;color:var(--text-secondary);margin:0 0 4px;" },
        `Spending by ${data.group_by}`,
      ),
      h("p", { style: "font-size:24px;font-weight:500;margin:0 0 12px;" }, money(data.grand_total ?? "0")),
      ...data.rows.map((row) =>
        h(
          "div",
          { style: "margin-bottom:12px;" },
          h(
            "div",
            { style: "display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;" },
            h("span", { style: "font-weight:500;" }, label(row.key)),
            h("span", { style: "color:var(--text-secondary);" }, money(row.total ?? "0")),
          ),
          h(
            "div",
            { style: "position:relative;height:10px;background:var(--surface-1);border-radius:5px;" },
            h("div", {
              style: `position:absolute;left:0;top:0;height:10px;border-radius:5px;background:${CHART.bar};width:${Math.max(2, (Math.abs(num(row.total ?? "0")) / maxTotal) * 100)}%;`,
            }),
          ),
        ),
      ),
    ),
  );
  void host;
}

function render(data: SpendingResult, host: WidgetHost): void {
  if (isBaseline(data)) renderBaseline(data, host);
  else renderClassic(data, host);
}

initWidget<SpendingResult>({ name: "spending", demo: DEMO, render });
