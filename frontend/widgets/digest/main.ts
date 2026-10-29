/* Digest widget (issue #77): the compact receipt behind the agent's morning
 * brief — one icon line per material item. Entity drill goes through the
 * host (get_transaction / model context); no other interactivity. */

import { initWidget, type WidgetHost } from "../lib/host";
import { clear, h, icon, money, shortDate } from "../lib/kit";
import "../lib/theme.css";

interface WhatsNewResult {
  cursor: string | null;
  transactions: {
    count: number;
    total: string;
    notable: { payee: string | null; description?: string | null; amount?: string; transaction_id: string }[];
    truncated: boolean;
  };
  matches: { name: string; amount: string; variance: string; match_id: string }[];
  matches_truncated: boolean;
  price_changes: { payee: string; delta: string; schedule_id: string }[];
  price_changes_truncated: boolean;
  forecast_flags: { account: string; low: string | null; date: string }[];
  upcoming: {
    count: number;
    total: string;
    items: { date: string; name: string; amount: string; transaction_id: string }[];
    truncated: boolean;
  };
  sync: { failures: number };
  events_truncated: boolean;
}

const DEMO: WhatsNewResult = {
  cursor: "evt-demo",
  transactions: {
    count: 2,
    total: "96.40",
    notable: [{ payee: "Whole Foods", amount: "-61.15", transaction_id: "t-1" }],
    truncated: false,
  },
  matches: [{ name: "Mortgage", amount: "-2400.00", variance: "0.00", match_id: "m-1" }],
  matches_truncated: false,
  price_changes: [{ payee: "YouTube Premium", delta: "2.00", schedule_id: "s-1" }],
  price_changes_truncated: false,
  forecast_flags: [{ account: "assets.checking", low: "-142.00", date: "2026-07-19" }],
  upcoming: { count: 3, total: "1042.39", items: [], truncated: false },
  sync: { failures: 0 },
  events_truncated: false,
};

function line(
  glyph: Parameters<typeof icon>[0],
  color: string,
  content: (Node | string)[],
  onclick?: () => void,
): HTMLElement {
  return h(
    "div",
    {
      style:
        "display:flex;gap:10px;align-items:flex-start;margin-bottom:10px;" +
        (onclick ? "cursor:pointer;" : ""),
      ...(onclick ? { onclick } : {}),
    },
    h("span", { style: "margin-top:1px;flex:none;" }, icon(glyph, color, 17)),
    h("span", { style: "font-size:14px;" }, ...content),
  );
}

function render(data: WhatsNewResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);

  const lines: HTMLElement[] = [];
  const tx = data.transactions;
  if (tx.count) {
    const names = tx.notable
      .map((n) => n.payee ?? n.description ?? "?")
      .slice(0, 2)
      .join(", ");
    lines.push(
      line("receipt", "var(--text-secondary)", [
        `${tx.count} new transaction${tx.count === 1 ? "" : "s"} · $${tx.total} `,
        names
          ? h("span", { style: "color:var(--text-muted);font-size:13px;" }, `(${names})`)
          : "",
      ]),
    );
  }
  for (const match of data.matches) {
    const clean = Number(match.variance) === 0;
    lines.push(
      line(clean ? "check" : "alert", `var(--text-${clean ? "success" : "warning"})`, [
        `${match.name} matched · ${money(match.amount)}${clean ? ", variance $0" : `, variance ${money(match.variance, { sign: true, cents: true })}`}`,
      ]),
    );
  }
  for (const change of data.price_changes) {
    lines.push(
      line("arrow-up", "var(--text-danger)", [
        `${change.payee} price change · ${Number(change.delta) >= 0 ? "+" : ""}${money(change.delta, { cents: true })}/mo`,
      ]),
    );
  }
  for (const flag of data.forecast_flags) {
    const leaf = flag.account.split(".").at(-1) ?? flag.account;
    lines.push(
      line("alert", "var(--text-warning)", [
        `${leaf.charAt(0).toUpperCase()}${leaf.slice(1)} projected${flag.low ? ` at ${money(flag.low)}` : " short"} on ${shortDate(flag.date)}`,
      ]),
    );
  }
  if (data.upcoming.count) {
    lines.push(
      line("clock", "var(--text-secondary)", [
        `This week: ${data.upcoming.count} bill${data.upcoming.count === 1 ? "" : "s"} expected · $${data.upcoming.total}`,
      ]),
    );
  }
  if (data.sync.failures) {
    lines.push(line("alert", "var(--text-danger)", [`${data.sync.failures} sync failure(s)`]));
  }
  if (lines.length === 0) {
    lines.push(line("check", "var(--text-success)", ["Quiet since your last check."]));
  }
  lines[lines.length - 1].style.marginBottom = "0";

  root.append(
    h(
      "div",
      { class: "card", style: "max-width:520px;" },
      h(
        "p",
        { style: "font-size:13px;color:var(--text-secondary);margin:0 0 12px;" },
        `What's new · ${new Date().toLocaleDateString("en-US", { month: "short", day: "numeric" })}`,
      ),
      ...lines,
    ),
  );
  void host;
}

initWidget<WhatsNewResult>({ name: "digest", demo: DEMO, render });
