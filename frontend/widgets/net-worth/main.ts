/* Net-worth widget (issue #75): headline number + period change, area chart
 * with a liabilities reference line, range pills re-calling the tool, and
 * attribution chips explaining what moved the number. Also renders the
 * point-mode payload (no series) as a compact card. */

import type { NetWorthPointResult, NetWorthResult, NetWorthSeriesResult } from "../lib/contracts";
import { initWidget, type WidgetHost } from "../lib/host";
import {
  CHART,
  areaPath,
  clear,
  h,
  linePath,
  linear,
  money,
  moneyCompact,
  monthYear,
  num,
  s,
  ticks,
} from "../lib/kit";
import "../lib/theme.css";

const W = 640;
const H = 210;
const PAD_L = 48;
const PAD_R = 14;
const PAD_T = 12;
const PAD_B = 22;

const PERIODS = ["90d", "1y", "all"];

const DEMO: NetWorthSeriesResult = {
  current: "448210.00",
  change: { period: "1y", delta: "35800.00", pct: "8.7" },
  series: [
    { date: "2025-07-07", assets: "698400.00", liabilities: "285990.00", net: "412410.00" },
    { date: "2025-08-07", assets: "701200.00", liabilities: "285100.00", net: "416100.00" },
    { date: "2025-09-07", assets: "699800.00", liabilities: "284300.00", net: "415500.00" },
    { date: "2025-10-07", assets: "706900.00", liabilities: "283500.00", net: "423400.00" },
    { date: "2025-11-07", assets: "705300.00", liabilities: "282600.00", net: "422700.00" },
    { date: "2025-12-07", assets: "712600.00", liabilities: "281800.00", net: "430800.00" },
    { date: "2026-01-07", assets: "710900.00", liabilities: "280900.00", net: "430000.00" },
    { date: "2026-02-07", assets: "718300.00", liabilities: "280100.00", net: "438200.00" },
    { date: "2026-03-07", assets: "716500.00", liabilities: "279200.00", net: "437300.00" },
    { date: "2026-04-07", assets: "722100.00", liabilities: "278400.00", net: "443700.00" },
    { date: "2026-05-07", assets: "721400.00", liabilities: "277500.00", net: "443900.00" },
    { date: "2026-06-07", assets: "724000.00", liabilities: "276700.00", net: "447300.00" },
    { date: "2026-07-07", assets: "724100.00", liabilities: "275890.00", net: "448210.00" },
  ],
  attribution: [
    { bucket: "market", delta: "21500.00" },
    { bucket: "loan_principal", delta: "10100.00" },
    { bucket: "cash", delta: "4200.00" },
  ],
  series_uri: "purser://net-worth/daily",
};

function isSeries(data: NetWorthResult): data is NetWorthSeriesResult {
  return "series" in data;
}

function humanBucket(bucket: string): string {
  return bucket.replace(/_/g, " ");
}

function renderPoint(data: NetWorthPointResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);
  const rows = data.breakdown.map((b, i) =>
    h(
      "div",
      {
        style:
          "display:flex;justify-content:space-between;font-size:13px;padding:6px 2px;" +
          (i === data.breakdown.length - 1 ? "" : "border-bottom:0.5px solid var(--border);"),
      },
      h("span", { style: "color:var(--text-secondary);" }, b.path),
      h("span", { style: "font-weight:500;" }, b.value ? money(b.value) : "—"),
    ),
  );
  root.append(
    h(
      "div",
      { class: "card" },
      h("p", { style: "font-size:13px;color:var(--text-secondary);margin:0;" }, `Net worth · ${data.as_of}`),
      h("p", { style: "font-size:24px;font-weight:500;margin:0 0 10px;" }, money(data.net_worth)),
      h(
        "p",
        { style: "font-size:13px;color:var(--text-secondary);margin:0 0 8px;" },
        `assets ${money(data.assets)} · liabilities ${money(data.liabilities)}`,
      ),
      ...rows,
    ),
  );
  void host;
}

function renderSeries(data: NetWorthSeriesResult, host: WidgetHost): void {
  const root = document.getElementById("root");
  if (!root) return;
  clear(root);

  const change = data.change;
  const up = Number(change.delta) >= 0;
  const pills = PERIODS.map((period) =>
    h(
      "button",
      {
        class: `pill${period === change.period ? " active" : ""}`,
        onclick: async () => {
          const next = await host.callTool<NetWorthResult>("net_worth", { period });
          if (next && isSeries(next)) {
            renderSeries(next, host);
            host.tellModel(
              `Viewing net worth over ${period}: ${money(next.current)} (${Number(next.change.delta) >= 0 ? "+" : ""}${money(next.change.delta)}).`,
            );
          }
        },
      },
      period,
    ),
  );

  const header = h(
    "div",
    { style: "display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px;" },
    h(
      "div",
      {},
      h("p", { style: "font-size:13px;color:var(--text-secondary);margin:0;" }, "Net worth"),
      h(
        "p",
        { style: "font-size:24px;font-weight:500;margin:0;" },
        money(data.current),
        " ",
        h(
          "span",
          {
            style: `font-size:13px;font-weight:400;color:var(--text-${up ? "success" : "danger"});`,
          },
          change.pct
            ? `${up ? "+" : ""}${change.pct}% ${change.period === "1y" ? "YoY" : `over ${change.period}`}`
            : `${up ? "+" : ""}${money(change.delta)} over ${change.period}`,
        ),
      ),
    ),
    h("div", { style: "display:flex;gap:4px;" }, ...pills),
  );

  /* chart */
  const nets = data.series.map((p) => num(p.net));
  const liabs = data.series.map((p) => num(p.liabilities));
  const lo = Math.min(...nets, ...liabs);
  const hi = Math.max(...nets, ...liabs);
  const pad = (hi - lo || 1) * 0.08;
  const y = linear(lo - pad, hi + pad, H - PAD_B, PAD_T);
  const x = linear(0, data.series.length - 1, PAD_L, W - PAD_R);

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Net worth over time" });
  for (const tick of ticks(lo, hi, 3)) {
    svg.append(
      s("line", { x1: PAD_L, y1: y(tick), x2: W - PAD_R, y2: y(tick), stroke: "var(--border)", "stroke-width": 0.5 }),
      s("text", { x: PAD_L - 6, y: y(tick) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--text-muted)" }, moneyCompact(tick)),
    );
  }
  const netPts: [number, number][] = nets.map((v, i) => [x(i), y(v)]);
  const liabPts: [number, number][] = liabs.map((v, i) => [x(i), y(v)]);
  svg.append(
    s("path", { d: areaPath(netPts, H - PAD_B), fill: CHART.positiveFill, opacity: 0.22 }),
    s("path", { d: linePath(netPts), fill: "none", stroke: CHART.positive, "stroke-width": 2, "stroke-linejoin": "round" }),
    s("polyline", {
      points: liabPts.map(([px, py]) => `${px},${py}`).join(" "),
      fill: "none",
      stroke: CHART.liabilities,
      "stroke-width": 1.5,
      "stroke-dasharray": "4 3",
    }),
    s(
      "text",
      { x: W - PAD_R - 4, y: y(liabs[liabs.length - 1]) - 6, "text-anchor": "end", "font-size": 11, fill: CHART.liabilities },
      `liabilities ${moneyCompact(liabs[liabs.length - 1])}`,
    ),
    s("circle", { cx: netPts[netPts.length - 1][0], cy: netPts[netPts.length - 1][1], r: 4, fill: CHART.positive }),
  );
  const mid = Math.floor((data.series.length - 1) / 2);
  for (const [idx, anchor] of [
    [0, "start"],
    [mid, "middle"],
    [data.series.length - 1, "end"],
  ] as const) {
    svg.append(
      s(
        "text",
        { x: x(idx as number), y: H - 6, "text-anchor": anchor as string, "font-size": 11, fill: "var(--text-muted)" },
        monthYear(data.series[idx as number].date),
      ),
    );
  }

  /* hover: assets/liabilities split for the nearest point */
  const hover = h("p", { style: "font-size:12px;color:var(--text-muted);margin:6px 0 0;min-height:16px;" }, "");
  svg.addEventListener("mousemove", (ev) => {
    const rect = (svg as unknown as SVGGraphicsElement).getBoundingClientRect();
    const rel = ((ev.clientX - rect.left) / rect.width) * W;
    const idx = Math.max(0, Math.min(data.series.length - 1, Math.round(((rel - PAD_L) / (W - PAD_L - PAD_R)) * (data.series.length - 1))));
    const point = data.series[idx];
    hover.textContent = `${monthYear(point.date)}: net ${money(point.net)} · assets ${money(point.assets)} · liabilities ${money(point.liabilities)}`;
  });
  svg.addEventListener("mouseleave", () => {
    hover.textContent = "";
  });

  /* attribution chips */
  const chips = h(
    "div",
    { style: "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center;" },
    h(
      "span",
      { style: "font-size:13px;color:var(--text-secondary);" },
      `This period ${up ? "+" : ""}${money(change.delta)}:`,
    ),
    ...data.attribution.map((entry) =>
      h(
        "button",
        {
          class: "badge badge-success",
          style: "border:none;cursor:pointer;font-family:inherit;",
          onclick: async () => {
            host.tellModel(
              `Drilling into the ${humanBucket(entry.bucket)} bucket: ${Number(entry.delta) >= 0 ? "+" : ""}${money(entry.delta)} over ${change.period}.`,
            );
            if (entry.bucket === "market") {
              await host.callTool("list_holdings", {});
            }
          },
        },
        `${humanBucket(entry.bucket)} ${Number(entry.delta) >= 0 ? "+" : ""}${money(entry.delta)}`,
      ),
    ),
  );

  root.append(h("div", { class: "card" }, header, svg, hover, chips));
}

function render(data: NetWorthResult, host: WidgetHost): void {
  if (isSeries(data)) renderSeries(data, host);
  else renderPoint(data, host);
}

initWidget<NetWorthResult>({ name: "net-worth", demo: DEMO, render });
