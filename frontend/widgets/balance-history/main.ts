/* Balance-history widget (issue #200): one account's balance over time as a
 * step line, with period pills, mover dots that drill into get_transaction,
 * a faint full-resolution underlay pulled from the purser:// resource, and
 * an optional dashed 30-day forecast tail.
 *
 * The forecast tail is fetched CLIENT-side through the host's callTool, not
 * merged into the tool payload server-side: the two shapes stay disjoint,
 * nothing about balance_history's contract changes, and the call inherits
 * the token scope model (guardrail 11). */

import type {
  BalanceHistoryFullSeries,
  BalanceHistoryMover,
  BalanceHistoryResult,
  ForecastDailySeries,
  ForecastResult,
  TransactionDetail,
} from "../lib/contracts";
import { balanceChart, CHART_W, type ChartMarker } from "../lib/chart";
import { initWidget, type WidgetHost } from "../lib/host";
import { CHART, clear, dayDiff, h, money, num, pathLabel, shortDate } from "../lib/kit";
import "../lib/theme.css";

const PERIODS = ["30d", "90d", "1y", "all"] as const;
const FORECAST_DAYS = 30;

function root(): HTMLElement {
  const node = document.getElementById("root");
  if (!node) throw new Error("no #root");
  return node;
}

/** Domain x is "days since the window start", so history and a forecast tail
 * share one axis without either needing to know the other's units. */
function xOf(start: string, iso: string): number {
  return dayDiff(start, iso);
}

/** Balance in effect on `iso`: the last sampled point at or before it.
 * Hand-rolled rather than Array.findLastIndex — the widgets tsconfig targets
 * a lib below es2023, and bumping it for one call is not worth it. */
function balanceOn(dates: string[], balances: string[], iso: string, fallback: string): string {
  let found = fallback;
  for (let i = 0; i < dates.length; i += 1) {
    if (dates[i] <= iso) found = balances[i];
    else break;
  }
  return found;
}

async function showMover(
  mover: BalanceHistoryMover,
  x: number,
  y: number,
  host: WidgetHost,
): Promise<void> {
  host.tellModel(
    `Looking at ${mover.payee ?? "a transaction"} ${money(mover.amount, { sign: true })} on ${mover.date}.`,
  );
  const detail = await host.callTool<TransactionDetail>("get_transaction", {
    transaction_id: mover.transaction_id,
  });
  const existing = document.getElementById("mover-pop");
  existing?.remove();
  const pop = h(
    "div",
    {
      id: "mover-pop",
      style:
        `position:absolute;left:${Math.min(Math.max(x - 90, 4), CHART_W - 190)}px;` +
        `top:${Math.max(y - 74, 4)}px;width:180px;padding:8px 10px;` +
        "background:var(--surface);border:1px solid var(--border);border-radius:8px;" +
        "box-shadow:0 4px 14px rgba(0,0,0,0.16);font-size:12px;z-index:2;",
    },
    h(
      "div",
      { style: "font-weight:500;margin-bottom:2px;" },
      mover.payee ?? detail?.description ?? "Transaction",
    ),
    h(
      "div",
      { style: "color:var(--text-muted);" },
      `${money(mover.amount, { sign: true })} · ${shortDate(mover.date)}`,
    ),
    ...(detail?.status
      ? [h("div", { style: "color:var(--text-muted);margin-top:2px;" }, detail.status)]
      : []),
  );
  pop.addEventListener("click", () => pop.remove());
  document.getElementById("chart-wrap")?.append(pop);
}

function render(data: BalanceHistoryResult, host: WidgetHost): void {
  const node = root();
  clear(node);

  /* header: account, current balance, change, period pills */
  const delta = num(data.change.delta);
  const pills = PERIODS.map((period) =>
    h(
      "button",
      {
        style:
          "font-size:11px;padding:3px 8px;border-radius:999px;cursor:pointer;" +
          "border:1px solid var(--border);" +
          (period === data.change.period
            ? "background:var(--text-primary);color:var(--surface);"
            : "background:transparent;color:var(--text-muted);"),
        onclick: () => {
          void host.callTool("balance_history", { account: data.account, period });
        },
      },
      period,
    ),
  );

  node.append(
    h(
      "div",
      { style: "display:flex;align-items:baseline;justify-content:space-between;gap:12px;" },
      h(
        "div",
        {},
        h("div", { style: "font-size:14px;font-weight:500;" }, pathLabel(data.account)),
        h(
          "div",
          { style: "font-size:22px;font-weight:600;margin-top:2px;" },
          money(data.current, { cents: true }),
        ),
        h(
          "div",
          {
            style:
              "font-size:12px;margin-top:2px;color:" +
              (delta >= 0 ? CHART.positive : "var(--text-secondary)"),
          },
          `${money(data.change.delta, { sign: true })}${data.change.pct ? ` (${data.change.pct}%)` : ""} over ${data.change.period}`,
        ),
      ),
      h("div", { style: "display:flex;gap:4px;align-self:flex-start;" }, ...pills),
    ),
  );

  /* chart */
  const { dates, balances } = data.series;
  const points: [number, number][] = dates.map((iso, i) => [
    xOf(data.start, iso),
    num(balances[i]),
  ]);
  const lastX = points.length ? points[points.length - 1][0] : 0;

  const markers: ChartMarker[] = data.movers.map((mover) => ({
    x: xOf(data.start, mover.date),
    // Place the dot on the line: the balance in effect at that date.
    value: num(balanceOn(dates, balances, mover.date, data.current)),
    kind: num(mover.amount) >= 0 ? "in" : "out",
    label: `${(mover.payee ?? "move").toLowerCase()} ${money(mover.amount, { sign: true })}`,
    title: `${mover.payee ?? "Transaction"} ${money(mover.amount, { sign: true })} · ${shortDate(mover.date)}`,
    onClick: () => {},
  }));
  if (data.low && data.low.balance !== data.current) {
    markers.push({
      x: xOf(data.start, data.low.date),
      value: num(data.low.balance),
      kind: "low",
      label: money(data.low.balance),
      title: `Low ${money(data.low.balance)} on ${shortDate(data.low.date)}`,
    });
  }

  const tickCount = Math.min(4, points.length);
  const xTicks = Array.from({ length: tickCount }, (_, i) =>
    Math.round((i / Math.max(1, tickCount - 1)) * lastX),
  );

  const wrap = h("div", { id: "chart-wrap", style: "position:relative;margin-top:10px;" });
  const { svg, scales } = balanceChart({
    points,
    xDomain: [0, lastX],
    xTicks,
    xLabel: (x) => shortDate(dates[Math.min(dates.length - 1, Math.round((x / (lastX || 1)) * (dates.length - 1)))] ?? data.start),
    reference: { value: 0, label: "zero", color: CHART.floor },
    markers,
    step: true,
    ariaLabel: `Balance history for ${pathLabel(data.account)} over ${data.change.period}`,
  });
  wrap.append(svg);
  node.append(wrap);

  /* mover dots are wired after draw so they can position their popover in
   * the chart's own coordinate space */
  const dots = svg.querySelectorAll("circle");
  data.movers.forEach((mover, i) => {
    const dot = dots[i];
    if (!dot) return;
    dot.addEventListener("click", () => {
      void showMover(
        mover,
        scales.x(xOf(data.start, mover.date)),
        scales.y(num(balanceOn(dates, balances, mover.date, data.current))),
        host,
      );
    });
  });

  /* footer: density provenance + the forecast toggle */
  const footer = h(
    "div",
    {
      style:
        "display:flex;align-items:center;justify-content:space-between;" +
        "margin-top:8px;font-size:11px;color:var(--text-muted);",
    },
    h(
      "span",
      {},
      `${data.density.points} points · ` +
        (data.density.interval_days === 1 ? "daily" : `every ${data.density.interval_days} days`) +
        ` · ${data.transactions} transaction${data.transactions === 1 ? "" : "s"}` +
        (data.method === "postings" ? "" : ` · ${data.method}`),
    ),
    h(
      "button",
      {
        style:
          "font-size:11px;padding:3px 8px;border-radius:999px;cursor:pointer;" +
          "border:1px solid var(--border);background:transparent;color:var(--text-muted);",
        onclick: () => void addForecast(data, host, wrap, points, lastX, markers, xTicks, dates),
      },
      `+${FORECAST_DAYS}d forecast`,
    ),
  );
  node.append(footer);

  /* progressive enhancement: overlay the full per-change series, faint.
   * Never required for first paint — a host without resource support, or a
   * denied read, simply keeps the sampled line. */
  void host.readResource(data.series_uri).then((raw) => {
    if (!raw) return;
    let full: BalanceHistoryFullSeries;
    try {
      full = JSON.parse(raw) as BalanceHistoryFullSeries;
    } catch {
      return;
    }
    if (!full.points?.length) return;
    const underlay: [number, number][] = [
      [0, num(full.opening)],
      ...full.points.map((p) => [xOf(data.start, p.date), num(p.balance)] as [number, number]),
    ];
    const redrawn = balanceChart({
      points,
      xDomain: [0, lastX],
      xTicks,
      xLabel: (x) => shortDate(dates[Math.min(dates.length - 1, Math.round((x / (lastX || 1)) * (dates.length - 1)))] ?? data.start),
      reference: { value: 0, label: "zero", color: CHART.floor },
      markers,
      step: true,
      underlay,
      ariaLabel: `Balance history for ${pathLabel(data.account)} over ${data.change.period}`,
    });
    clear(wrap);
    wrap.append(redrawn.svg);
  });
}

async function addForecast(
  data: BalanceHistoryResult,
  host: WidgetHost,
  wrap: HTMLElement,
  points: [number, number][],
  lastX: number,
  markers: ChartMarker[],
  xTicks: number[],
  dates: string[],
): Promise<void> {
  const forecast = await host.callTool<ForecastResult>("forecast_balance", {
    account: data.account,
    days: FORECAST_DAYS,
  });
  if (!forecast) return;
  host.tellModel(`Showing the next ${FORECAST_DAYS} days of ${data.account} alongside its history.`);
  let tail: [number, number][] = forecast.events.map((e) => [
    lastX + dayDiff(forecast.as_of, e.date),
    num(e.balance_after),
  ]);
  if (forecast.omitted) {
    /* Not every movement is listed (#354): draw the tail from every day of
     * the forecast series when the host can read it. */
    const text = await host.readResource(forecast.daily_uri);
    if (text) {
      try {
        const daily = JSON.parse(text) as ForecastDailySeries;
        tail = daily.days.map((d) => [lastX + dayDiff(forecast.as_of, d.date), num(d.balance)]);
      } catch {
        /* keep the listed events */
      }
    }
  }
  const combined = [...points, ...tail].sort((a, b) => a[0] - b[0]);
  const maxX = combined.length ? combined[combined.length - 1][0] : lastX;
  const { svg } = balanceChart({
    points: combined,
    xDomain: [0, maxX],
    xTicks,
    xLabel: (x) =>
      x <= lastX
        ? shortDate(dates[Math.min(dates.length - 1, Math.round((x / (lastX || 1)) * (dates.length - 1)))] ?? data.start)
        : "",
    reference: { value: 0, label: "zero", color: CHART.floor },
    markers,
    step: true,
    splitAt: lastX,
    ariaLabel: `Balance history and ${FORECAST_DAYS}-day forecast for ${pathLabel(data.account)}`,
  });
  clear(wrap);
  wrap.append(svg);
}

initWidget<BalanceHistoryResult>({
  name: "balance-history",
  demo: {
    account: "assets.checking",
    start: "2026-07-10",
    end: "2026-08-09",
    currency: "USD",
    method: "postings",
    current: "3448.00",
    change: { period: "30d", delta: "-612.40", pct: "-15.1" },
    low: { date: "2026-07-19", balance: "1842.10" },
    high: { date: "2026-07-10", balance: "4060.40" },
    density: { interval_days: 1, points: 5 },
    transactions: 12,
    series: {
      dates: ["2026-07-10", "2026-07-19", "2026-07-26", "2026-08-02", "2026-08-09"],
      balances: ["4060.40", "1842.10", "3990.10", "3702.55", "3448.00"],
    },
    movers: [
      {
        date: "2026-07-19",
        payee: "Bank Mortgage",
        amount: "-2400.00",
        transaction_id: "00000000-0000-0000-0000-000000000001",
      },
      {
        date: "2026-07-26",
        payee: "Employer Inc",
        amount: "2148.00",
        transaction_id: "00000000-0000-0000-0000-000000000002",
      },
    ],
    series_uri: "purser://balance-history/assets.checking/30d",
  },
  render,
});

