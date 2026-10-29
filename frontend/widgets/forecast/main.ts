/* Forecast widget (issue #73): step line over the horizon where every
 * vertical move is an expected event; dashed account-floor line; shaded
 * below-zero region; shortfall banner naming the causal event. Horizon
 * pills re-call forecast_balance via the host; event dots drill into
 * get_transaction; selections land in the model context. When estimates
 * carry a range (#247), the daily series' low/high draw a shaded band
 * behind the line, and the banner warns when only the band crosses the
 * floor. */

import type { ForecastDailySeries, ForecastEvent, ForecastResult, TransactionDetail } from "../lib/contracts";
import { initWidget, type WidgetHost } from "../lib/host";
import { balanceChart, CHART_H, CHART_W, type ChartScales } from "../lib/chart";
import {
  CHART,
  addDays,
  clear,
  dayDiff,
  h,
  icon,
  linePath,
  money,
  num,
  pathLabel,
  s,
  shortDate,
  stepPath,
} from "../lib/kit";
import "../lib/theme.css";

const W = CHART_W;
const PAD_B = 26;

const HORIZONS = [30, 90, 365];

const DEMO: ForecastResult = {
  account: "assets.checking",
  as_of: "2026-07-08",
  horizon_days: 30,
  start_balance: "3448.00",
  end_balance: "4258.00",
  min_balance: "500.00",
  low: { date: "2026-07-19", balance: "-142.00" },
  events: [
    { date: "2026-07-14", name: "Mortgage", amount: "-2400.00", balance_after: "1048.00", transaction_id: null },
    { date: "2026-07-19", name: "Car insurance", amount: "-1190.00", balance_after: "-142.00", transaction_id: null },
    { date: "2026-07-20", name: "Payroll", amount: "3100.00", balance_after: "2958.00", transaction_id: null },
    { date: "2026-08-01", name: "Rent", amount: "-1800.00", balance_after: "1158.00", transaction_id: null },
    { date: "2026-08-03", name: "Payroll", amount: "3100.00", balance_after: "4258.00", transaction_id: null },
  ],
  coverage: { schedules_active: 4, instances: 5, projected: 0, suggestions_open: 0, matches_pending: 0 },
  truncated: false,
  daily_uri: "purser://forecast/assets.checking/30d",
};

function seriesPoints(data: ForecastResult): [number, number][] {
  /* Reconstruct the step series from the events array — the events ARE the
   * chart (issue #73). Points in (dayOffset, balance) space. */
  const points: [number, number][] = [[0, num(data.start_balance)]];
  for (const event of data.events) {
    points.push([dayDiff(data.as_of, event.date), num(event.balance_after)]);
  }
  points.push([data.horizon_days, points[points.length - 1][1]]);
  return points;
}

/** The band from the full daily series (decision 85), [day, low, high] —
 * null when the forecast has none. */
function bandOf(daily: ForecastDailySeries, asOf: string): [number, number, number][] | null {
  const rows = daily.days.filter((d) => d.low !== undefined && d.high !== undefined);
  if (rows.length < 2) return null;
  return rows.map((d) => [dayDiff(asOf, d.date), num(d.low!), num(d.high!)]);
}

/** The listed ranged event on or before the pessimistic low whose band
 * costs the most against its point: an outflow coming in high, an inflow low. */
function bandDriver(data: ForecastResult): ForecastEvent | undefined {
  const until = data.low.range?.low_date;
  if (!until) return undefined;
  return data.events
    .filter((e) => e.range && e.date <= until)
    .sort((a, b) => num(b.amount) - num(b.range!.low) - (num(a.amount) - num(a.range!.low)))[0];
}

function eventTitle(event: ForecastEvent): string {
  const flag = event.overdue ? " · overdue" : event.projected ? " · projected" : "";
  const basis = event.basis === "statement" ? " · statement balance" : event.basis === "override" ? " · your amount" : "";
  const range = event.range
    ? ` · could be ${money(event.range.low, { sign: true })} to ${money(event.range.high, { sign: true })}`
    : "";
  return `${event.name} ${money(event.amount, { sign: true })} · ${shortDate(event.date)}${flag}${basis}${range}`;
}

function causalStory(data: ForecastResult): string {
  const base = `Projected low of ${money(data.low.balance)} on ${shortDate(data.low.date)}`;
  const atLow = [...data.events].reverse().find((e) => e.balance_after === data.low.balance);
  if (!atLow) return base;
  const rescue = data.events.find((e) => num(e.amount) > 0 && dayDiff(atLow.date, e.date) > 0);
  if (rescue) {
    const gap = dayDiff(atLow.date, rescue.date);
    const when = gap === 1 ? "one day" : `${gap} days`;
    return `${base} — ${atLow.name.toLowerCase()} lands ${when} before ${rescue.name.toLowerCase()}`;
  }
  return `${base} — after ${atLow.name.toLowerCase()} ${money(atLow.amount, { sign: true })}`;
}

function render(data: ForecastResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);

  const floor = data.min_balance === null ? 0 : num(data.min_balance);
  const shortfall = num(data.low.balance) < floor;

  /* header ---------------------------------------------------------------- */
  const pills = HORIZONS.map((days) =>
    h(
      "button",
      {
        class: `pill${days === data.horizon_days ? " active" : ""}`,
        onclick: async () => {
          const next = await host.callTool<ForecastResult>("forecast_balance", {
            account: data.account,
            days,
          });
          if (next) {
            render(next, host);
            host.tellModel(
              `Viewing the ${days}-day forecast for ${next.account}: low ${money(next.low.balance)} on ${next.low.date}.`,
            );
          }
        },
      },
      `${days}d`,
    ),
  );
  const header = h(
    "div",
    { style: "display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;" },
    h(
      "div",
      { style: "display:flex;align-items:center;gap:8px;" },
      h("span", { style: "font-size:14px;font-weight:500;" }, pathLabel(data.account)),
      h("span", { style: "font-size:12px;color:var(--text-muted);" }, `as of ${shortDate(data.as_of)}`),
    ),
    h("div", { style: "display:flex;gap:4px;" }, ...pills),
  );

  /* chart ----------------------------------------------------------------- */
  /* Chrome (scales, gridlines, axis labels, below-zero shading, the dashed
   * floor and the step line) comes from the shared lib; the bespoke event
   * dots and story labels below stay here, because their placement rules are
   * forecast-specific and sharing them would just move the specialness. */
  const points = seriesPoints(data);
  const xTickEvery = Math.max(7, Math.round(data.horizon_days / 4 / 7) * 7);
  const xTicks: number[] = [];
  for (let day = 0; day <= data.horizon_days; day += xTickEvery) xTicks.push(day);

  /* Drawn again, with the band, once the daily series arrives: the band can
   * reach past the point line, so the scales must include it. */
  const drawChart = (band?: [number, number, number][]): { svg: SVGElement; scales: ChartScales } => {
    const chart = balanceChart({
      points,
      xDomain: [0, data.horizon_days],
      xTicks,
      xLabel: (day) => shortDate(addDays(data.as_of, day)),
      reference: { value: floor, label: `min balance ${money(String(floor))}`, color: CHART.floor },
      step: true,
      band,
      ariaLabel: `Balance forecast for ${pathLabel(data.account)}`,
    });
    drawEvents(chart.svg, chart.scales);
    return chart;
  };

  /* event dots + the three story labels (low, biggest out, biggest in) */
  const biggestOut = [...data.events].filter((e) => num(e.amount) < 0).sort((a, b) => num(a.amount) - num(b.amount))[0];
  const biggestIn = [...data.events].filter((e) => num(e.amount) > 0).sort((a, b) => num(b.amount) - num(a.amount))[0];
  const drawEvents = (svg: SVGElement, scales: ChartScales): void => {
    const xScale = scales.x;
    const yScale = scales.y;
    for (const event of data.events) {
      const x = xScale(dayDiff(data.as_of, event.date));
      const y = yScale(num(event.balance_after));
      const isLow = event.balance_after === data.low.balance && num(data.low.balance) < floor;
      const color = isLow ? CHART.danger : num(event.amount) > 0 ? CHART.positive : CHART.line;
      /* A projected event is a rule occurrence with no row yet (#235): a
       * hollow dot says "expected by the rule", a filled one "an instance". */
      const dot = s("circle", {
        cx: x,
        cy: y,
        r: isLow ? 4.5 : 4,
        fill: event.projected ? "var(--surface-1)" : color,
        stroke: color,
        "stroke-width": event.projected ? 1.5 : 0,
        style: event.transaction_id ? "cursor:pointer;" : "",
        onclick: () => void showEvent(event, x, host),
      });
      dot.append(s("title", {}, eventTitle(event)));
      svg.append(dot);
      if (isLow) {
        svg.append(
          s("text", { x, y: Math.min(y + 21, CHART_H - PAD_B - 2), "text-anchor": "middle", "font-size": 11, fill: CHART.danger }, money(event.balance_after)),
        );
      } else if (event === biggestOut) {
        svg.append(
          s("text", { x, y: y - 11, "text-anchor": "middle", "font-size": 11, fill: "var(--text-secondary)" }, `${event.name.toLowerCase()} ${money(event.amount, { sign: true })}`),
        );
      } else if (event === biggestIn) {
        svg.append(
          s("text", { x: Math.min(x + 12, W - 80), y: y - 8, "font-size": 11, fill: "var(--text-secondary)" }, `${event.name.toLowerCase()} ${money(event.amount, { sign: true })}`),
        );
      }
    }
  };
  let chart = drawChart();

  /* banner ---------------------------------------------------------------- */
  const coverage = data.coverage;
  const unmodeled = coverage !== undefined && coverage.schedules_active === 0 && data.events.length === 0;
  const banner = unmodeled
    ? h(
        "div",
        { class: "banner banner-muted" },
        icon("alert", "var(--text-muted)", 16),
        h(
          "span",
          {},
          `No schedules are modeled on this account — the line is flat by construction.` +
            (coverage.suggestions_open > 0
              ? ` ${coverage.suggestions_open} suggestion${coverage.suggestions_open === 1 ? "" : "s"} await a decision.`
              : ""),
        ),
      )
    : shortfall
    ? h(
        "div",
        { class: "banner banner-danger" },
        icon("alert", "currentColor", 18),
        h("span", {}, causalStory(data)),
      )
    : data.low.range && num(data.low.range.low) < floor
    ? h(
        "div",
        { class: "banner banner-warning" },
        icon("alert", "currentColor", 16),
        h("span", {}, bandStory(data, floor)),
      )
    : h(
        "div",
        { class: "banner banner-muted" },
        icon("check", "var(--text-success)", 16),
        h(
          "span",
          {},
          `Lowest point ${money(data.low.balance)} on ${shortDate(data.low.date)} — stays above the ${money(String(floor))} floor.`,
        ),
      );

  const card = h("div", { class: "card" }, header, chart.svg, banner);
  if (coverage !== undefined && !unmodeled) {
    card.append(
      h(
        "p",
        { style: "font-size:12px;color:var(--text-muted);margin:8px 0 0;" },
        `Modeled from ${coverage.schedules_active} schedule${coverage.schedules_active === 1 ? "" : "s"}: ` +
          `${coverage.instances} instance${coverage.instances === 1 ? "" : "s"}, ${coverage.projected} projected` +
          (coverage.suggestions_open > 0 ? ` · ${coverage.suggestions_open} suggestion${coverage.suggestions_open === 1 ? "" : "s"} open` : "") +
          (coverage.matches_pending > 0 ? ` · ${coverage.matches_pending} awaiting match confirmation` : "") +
          ".",
      ),
    );
  }
  const omitted = data.omitted;
  if (omitted && omitted.count > 0) {
    /* #354: the payload lists what fits its budget; omitted accounts for the
     * rest, so the footnote says what is not itemized and why. */
    card.append(
      h(
        "p",
        { style: "font-size:12px;color:var(--text-muted);margin:8px 0 0;" },
        `${omitted.count} more movement${omitted.count === 1 ? "" : "s"} (net ${money(omitted.net, { sign: true })}) ` +
          `not itemized${data.truncated ? "" : " (below the materiality threshold)"}; ` +
          "the full daily series draws the line when the host can read it.",
      ),
    );
  }
  root.append(card);

  /* progressive enhancement: the full daily series, when the host can read
   * it (resources/read, else the read_series tool) — never required for
   * first paint. */
  void host.readResource(data.daily_uri).then((text) => {
    if (!text) return;
    try {
      const daily = JSON.parse(text) as ForecastDailySeries;
      const band = bandOf(daily, data.as_of);
      if (band) {
        const banded = drawChart(band);
        chart.svg.replaceWith(banded.svg);
        chart = banded;
      }
      const { svg } = chart;
      const xScale = chart.scales.x;
      const yScale = chart.scales.y;
      const pts: [number, number][] = daily.days.map((d) => [
        xScale(dayDiff(data.as_of, d.date)),
        yScale(num(d.balance)),
      ]);
      const line = svg.querySelector('[data-role="line"]');
      if (omitted && line && pts.length) {
        /* Movements were left out: a step line rebuilt from the listed events
         * folds each one into the next listed step and blames it on that
         * event. Every day's balance draws the true line instead. */
        line.setAttribute(
          "d",
          stepPath([
            [xScale(0), yScale(num(data.start_balance))],
            ...pts,
            [xScale(data.horizon_days), pts[pts.length - 1][1]],
          ]),
        );
        return;
      }
      svg.insertBefore(
        s("path", { d: linePath(pts), fill: "none", stroke: CHART.line, "stroke-width": 1, opacity: 0.35 }),
        line,
      );
    } catch {
      /* enhancement only */
    }
  });
}

function bandStory(data: ForecastResult, floor: number): string {
  const range = data.low.range!;
  const driver = bandDriver(data);
  const cause = driver ? ` if ${driver.name.toLowerCase()} comes in ${num(driver.amount) < 0 ? "high" : "low"}` : "";
  return (
    `Lowest point ${money(data.low.balance)} on ${shortDate(data.low.date)} stays above the ${money(String(floor))} floor, ` +
    `but could dip to ${money(range.low)} on ${shortDate(range.low_date)}${cause}.`
  );
}

async function showEvent(event: ForecastEvent, x: number, host: WidgetHost): Promise<void> {
  host.tellModel(
    `Selected the expected ${event.name} of ${money(event.amount, { sign: true })} on ${event.date}.`,
  );
  if (!event.transaction_id) return;
  const detail = await host.callTool<TransactionDetail>("get_transaction", {
    transaction_id: event.transaction_id,
  });
  if (!detail) return;
  document.getElementById("purser-popover")?.remove();
  const pop = h(
    "div",
    {
      id: "purser-popover",
      style:
        `position:absolute;left:${Math.min(Math.max(x, 60), W - 180)}px;top:70px;z-index:10;` +
        "background:var(--surface-2);border:0.5px solid var(--border);border-radius:8px;" +
        "padding:10px 12px;box-shadow:0 4px 16px rgba(0,0,0,0.12);font-size:13px;max-width:220px;",
      onclick: (ev: Event) => ev.stopPropagation(),
    },
    h("p", { style: "margin:0 0 4px;font-weight:500;" }, detail.payee ?? event.name),
    h("p", { style: "margin:0;color:var(--text-secondary);" }, `${shortDate(detail.date)} · ${detail.status}`),
    h("p", { style: "margin:4px 0 0;color:var(--text-secondary);" }, money(event.amount, { sign: true })),
  );
  document.body.append(pop);
  window.setTimeout(() => document.addEventListener("click", () => pop.remove(), { once: true }), 0);
}

initWidget<ForecastResult>({ name: "forecast", demo: DEMO, render });
