/* Recurring radar view (issues #74, #347): metric strip (monthly total,
 * active count, 12-month price changes), one row per merchant with change
 * badges, and a footer counting the schedule suggestions that await a
 * decision. Row click pulls recent merchant history into the model context.
 *
 * Every row is an ACTIVE schedule: `source` is provenance (manual, detected,
 * agent), never a pending state — a schedule accepted from a suggestion is
 * stored with source "detected" and is as active as any other. Patterns that
 * still need a decision are suggestions, counted by `suggestions_open`; the
 * footer only points at them, because deciding one is a commit write that
 * needs its id (list_suggestions / decide_suggestion). Kept apart from
 * main.ts so it renders in vitest without booting a host. */

import type { RecurringItem, RecurringResult, SearchResult } from "../lib/contracts";
import type { WidgetHost } from "../lib/host";
import { clear, h, money, shortDate } from "../lib/kit";

function metric(label: string, value: string, danger = false): HTMLElement {
  return h(
    "div",
    { class: "metric" },
    h("p", { class: "label" }, label),
    h("p", { class: "val", style: danger ? "color:var(--text-danger);" : "" }, value),
  );
}

export function row(item: RecurringItem, host: WidgetHost, last: boolean): HTMLElement {
  const cells: (HTMLElement | null)[] = [
    h("span", { style: "flex:1;font-size:14px;font-weight:500;" }, item.payee),
  ];
  if (item.last_change && Number(item.last_change.delta) > 0) {
    cells.push(
      h(
        "span",
        { class: "badge badge-danger" },
        `+$${item.last_change.delta} in ${shortDate(item.last_change.date).split(" ")[0]}`,
      ),
    );
  }
  cells.push(
    h("span", { style: "width:110px;font-size:13px;color:var(--text-muted);" }, item.cadence),
    h(
      "span",
      { style: "width:86px;font-size:13px;color:var(--text-secondary);" },
      item.next_due ? `due ${shortDate(item.next_due)}` : "",
    ),
    h(
      "span",
      { style: "width:72px;text-align:right;font-size:14px;font-weight:500;" },
      money(item.amount, { cents: true }),
    ),
  );
  return h(
    "div",
    {
      style:
        "display:flex;align-items:center;gap:12px;padding:10px 2px;cursor:pointer;" +
        (last ? "" : "border-bottom:0.5px solid var(--border);"),
      onclick: async () => {
        host.tellModel(`Looking at the ${item.payee} schedule (${item.cadence}, ${money(item.amount, { cents: true })}).`);
        const history = await host.callTool<SearchResult>("search_transactions", {
          payee: item.payee,
          limit: 5,
        });
        if (history?.items?.length) {
          const lines = history.items
            .map((t) => `${t.date} ${t.amount ?? ""}`)
            .join("; ");
          host.tellModel(`Recent ${item.payee} history: ${lines}.`);
        }
      },
    },
    ...cells,
  );
}

function footnote(text: string): HTMLElement {
  return h("p", { style: "font-size:12px;color:var(--text-muted);margin:8px 0 0;" }, text);
}

export function render(data: RecurringResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);

  const changes = data.changes_12mo;
  const strip = h(
    "div",
    {
      style:
        "display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px;",
    },
    metric("Monthly recurring", money(data.monthly_total, { cents: true })),
    metric("Active schedules", String(data.count)),
    metric(
      "Price changes, 12 mo",
      changes.count ? `${changes.count} · ${money(changes.delta, { sign: true, cents: true })}` : "0",
      changes.count > 0 && Number(changes.delta) > 0,
    ),
  );

  const list = h("div", { style: "border-top:0.5px solid var(--border);" });
  data.items.forEach((item, i) => list.append(row(item, host, i === data.items.length - 1)));

  const card = h("div", { class: "card" }, strip, list);
  if (data.truncated) {
    card.append(
      footnote(`Showing the ${data.items.length} largest of ${data.count} — totals cover everything.`),
    );
  }
  if (data.suggestions_open > 0) {
    const n = data.suggestions_open;
    card.append(
      footnote(
        `${n} ${n === 1 ? "suggestion awaits" : "suggestions await"} a decision — ask to list them.`,
      ),
    );
  }
  root.append(card);
}
