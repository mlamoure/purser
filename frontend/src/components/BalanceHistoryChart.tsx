import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatDate } from "../lib/dates";
import { compareMoney, formatMoney, formatMoneyCompact, moneySign, negateMoney } from "../lib/money";
import {
  chartGeometry,
  dayNumber,
  isLiabilityType,
  nearestIndex,
  shownValue,
  type ChartPoint,
} from "../lib/balanceChart";
import { cn } from "../lib/utils";
import { Card, ErrorText } from "./ui";

type AccountOut = components["schemas"]["AccountOut"];
type BalanceHistoryOut = components["schemas"]["BalanceHistoryOut"];

export const RANGES = ["1m", "3m", "6m", "ytd", "1y", "5y", "all"] as const;
export type ChartRange = (typeof RANGES)[number];

const RANGE_LABELS: Record<ChartRange, string> = {
  "1m": "1M",
  "3m": "3M",
  "6m": "6M",
  ytd: "YTD",
  "1y": "1Y",
  "5y": "5Y",
  all: "All",
};

const RANGE_PHRASES: Record<ChartRange, string> = {
  "1m": "past month",
  "3m": "past 3 months",
  "6m": "past 6 months",
  ytd: "this year",
  "1y": "past year",
  "5y": "past 5 years",
  all: "all time",
};

const METHOD_HINTS: Record<string, string> = {
  "postings+holdings": "Cash and holdings",
  valuation: "From valuations",
};

const PAD = { t: 20, r: 12, b: 26, l: 12 };

/** "12.50" -> "12.5", "10.00" -> "10", "0.00" -> "0": a percent the API
 * sends at two places, without its trailing zeros. */
export function trimZeros(value: string): string {
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

/** "Nov 3" for a short window, "Nov 2025" for a long one. */
function axisDate(iso: string, spanDays: number): string {
  const full = formatDate(iso); // "Nov 3, 2025"
  const [monthDay, year] = full.split(", ");
  if (!year) return full;
  return spanDays > 200 ? `${monthDay.split(" ")[0]} ${year}` : monthDay;
}

/** The container's width, followed as it resizes (720 where ResizeObserver
 * is missing, as in tests), so the SVG draws at 1:1 and text stays crisp. */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry.contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** The account page's balance history (#594): the reported end-of-day
 * balance — holdings included for brokerage, valuations for loans — drawn as
 * a smooth line over a gradient, with range chips, the change over the
 * window, low/high marks and a crosshair. The line draws itself in when a
 * range loads (skipped under prefers-reduced-motion). A liability charts
 * what is owed, so paying it down reads as a line falling toward zero.
 * Clicking a day (or Enter on it) hands that date to `onPickDate`. */
export function BalanceHistoryChart({
  account,
  onPickDate,
}: {
  account: AccountOut;
  onPickDate?: (date: string) => void;
}) {
  const [range, setRange] = useState<ChartRange>("1y");
  const [active, setActive] = useState<number | null>(null);
  const [containerRef, width] = useWidth<HTMLDivElement>();
  const gradientId = useId().replace(/:/g, "");
  const owed = isLiabilityType(account.type);
  const currency = account.currency;
  const money = (v: string | null | undefined) => formatMoney(v, { currency });

  const history = useQuery({
    queryKey: ["balance-history", account.id, range],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts/{account_id}/balance/history", {
        params: { path: { account_id: account.id }, query: { range } },
      });
      if (error) throw new Error(errorDetail(error));
      return data as BalanceHistoryOut;
    },
    placeholderData: (previous) => previous,
  });
  const data = history.data;
  const height = width < 520 ? 180 : 220;

  const view = useMemo(() => {
    if (!data || data.points.length < 2 || data.first_activity == null) return null;
    const points: ChartPoint[] = data.points.map((p) => ({ date: p.date, balance: shownValue(p.balance, owed) }));
    const references: string[] = [];
    const floor = !owed && account.min_balance ? account.min_balance : null;
    if (floor) references.push(floor);
    const geometry = chartGeometry(points, { width, height, pad: PAD }, references);
    // Zero gets a line only when the series actually crosses it.
    const crossesZero = moneySign(geometry.lo) < 0 && moneySign(geometry.hi) > 0;
    const pick = (p: { date: string } | null | undefined) =>
      p ? points.findIndex((q) => q.date === p.date) : -1;
    // Owed flips the sign, so the ledger's low is the most owed.
    const lowIndex = pick(owed ? data.high : data.low);
    const highIndex = pick(owed ? data.low : data.high);
    const spanDays = dayNumber(data.end) - dayNumber(data.start);
    return { points, geometry, floor, crossesZero, lowIndex, highIndex, spanDays };
  }, [data, owed, account.min_balance, width, height]);

  // Replay the draw-in once per range, not on a background refetch.
  const [drawKey, setDrawKey] = useState(`${account.id}:${range}`);
  useEffect(() => {
    if (data && data.range === range) setDrawKey(`${account.id}:${range}`);
  }, [data, range, account.id]);
  useEffect(() => setActive(null), [range]);

  const change = data ? (owed ? negateMoney(data.change) : data.change) : null;
  // A window that opens near zero makes any percent absurd (+31,864%), so
  // past 1,000% the amount speaks alone.
  const rawPct = data?.change_pct ?? null;
  const changePct =
    rawPct != null && compareMoney(rawPct.replace(/^-/, ""), "1000") < 0
      ? owed
        ? negateMoney(rawPct)
        : rawPct
      : null;
  // Good news is green: a balance going up, or what is owed going down.
  const changeGood = change != null && (owed ? moneySign(change) < 0 : moneySign(change) > 0);
  const changeFlat = change == null || moneySign(change) === 0;

  const move = (event: PointerEvent<SVGSVGElement>) => {
    if (!view) return;
    const box = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - box.left) / (box.width || width)) * width;
    setActive(nearestIndex(view.geometry.xy, px));
  };
  const keys = (event: KeyboardEvent<SVGSVGElement>) => {
    if (!view) return;
    const last = view.points.length - 1;
    const current = active ?? last;
    const step = event.shiftKey ? 7 : 1;
    if (event.key === "ArrowLeft") setActive(Math.max(0, current - step));
    else if (event.key === "ArrowRight") setActive(Math.min(last, current + step));
    else if (event.key === "Home") setActive(0);
    else if (event.key === "End") setActive(last);
    else if (event.key === "Enter" && active != null) onPickDate?.(view.points[active].date);
    else if (event.key === "Escape") setActive(null);
    else return;
    event.preventDefault();
  };

  const hovered = view && active != null ? view.points[active] : null;
  const hoveredXY = view && active != null ? view.geometry.xy[active] : null;
  const summary =
    view && data
      ? `${owed ? "Amount owed" : "Balance"} ${RANGE_PHRASES[range]}: ${money(view.points[0].balance)} on ${formatDate(view.points[0].date)} to ${money(view.points[view.points.length - 1].balance)} on ${formatDate(data.end)}` +
        (view.lowIndex >= 0 ? `, low ${money(view.points[view.lowIndex].balance)} on ${formatDate(view.points[view.lowIndex].date)}` : "") +
        (view.highIndex >= 0 ? `, high ${money(view.points[view.highIndex].balance)} on ${formatDate(view.points[view.highIndex].date)}` : "") +
        ". Use the arrow keys to read each day."
      : "";

  return (
    <Card className="overflow-hidden" role="region" aria-label="Balance history">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            {owed ? "Amount owed" : "Balance history"}
            {data && METHOD_HINTS[data.method] && (
              <span className="text-xs"> · {METHOD_HINTS[data.method]}</span>
            )}
          </p>
          {/* The hovered day replaces the window's change; both live here so
              the line under the pointer never hides behind a tooltip. */}
          <div className="mt-1 flex min-h-7 flex-wrap items-baseline gap-x-2" aria-live="polite">
            {hovered ? (
              <>
                <span className="font-mono text-lg font-semibold">{money(hovered.balance)}</span>
                <span className="text-sm text-muted-foreground">{formatDate(hovered.date)}</span>
              </>
            ) : data && change != null ? (
              <>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-sm font-semibold",
                    changeFlat
                      ? "bg-muted text-muted-foreground"
                      : changeGood
                        ? "bg-success-soft text-success"
                        : "bg-destructive-soft text-destructive",
                  )}
                >
                  {!changeFlat &&
                    (moneySign(change) > 0 ? (
                      <ArrowUpRight aria-hidden="true" className="h-3.5 w-3.5" />
                    ) : (
                      <ArrowDownRight aria-hidden="true" className="h-3.5 w-3.5" />
                    ))}
                  {formatMoney(change, { currency, signDisplay: "always" })}
                  {changePct != null && ` (${moneySign(changePct) > 0 ? "+" : ""}${trimZeros(changePct)}%)`}
                </span>
                <span className="text-sm text-muted-foreground">{RANGE_PHRASES[range]}</span>
              </>
            ) : null}
          </div>
        </div>
        <div role="group" aria-label="Range" className="flex shrink-0 gap-0.5 rounded-full bg-muted p-0.5">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={r === range}
              onClick={() => setRange(r)}
              className={cn(
                "rounded-full px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
                r === range && "bg-card text-foreground shadow-sm",
              )}
            >
              {RANGE_LABELS[r]}
            </button>
          ))}
        </div>
      </div>

      <div ref={containerRef} className="relative mt-3" style={{ height }}>
        {history.error && <ErrorText>{`Could not load the balance history: ${history.error.message}`}</ErrorText>}
        {!data && history.isLoading && (
          <div aria-hidden="true" className="h-full w-full animate-pulse rounded-lg bg-muted" />
        )}
        {data && !view && (
          <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
            No balance history yet.
          </p>
        )}
        {view && (
          <svg
            key={drawKey}
            width={width}
            height={height}
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label={summary}
            tabIndex={0}
            className="block cursor-crosshair touch-pan-y rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onPointerMove={move}
            onPointerDown={move}
            onPointerLeave={() => setActive(null)}
            onClick={() => hovered && onPickDate?.(hovered.date)}
            onKeyDown={keys}
            onBlur={() => setActive(null)}
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.28} />
                <stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0} />
              </linearGradient>
            </defs>

            {/* Gridlines at the window's high and low, labelled with those
                exact strings; zero and the floor when they matter. */}
            {[view.geometry.hi, view.geometry.lo].map((v, i) => (
              <g key={i} className="text-muted-foreground">
                <line
                  x1={PAD.l}
                  x2={width - PAD.r}
                  y1={view.geometry.y(v)}
                  y2={view.geometry.y(v)}
                  stroke="var(--color-border)"
                />
                {/* The high's label sits above its line and the low's below,
                    each haloed in the card colour so the curve never runs
                    through the digits. */}
                <text
                  x={PAD.l}
                  y={view.geometry.y(v) + (i === 0 ? -5 : 12)}
                  fontSize={10}
                  fill="currentColor"
                  stroke="var(--color-card)"
                  strokeWidth={3}
                  paintOrder="stroke"
                >
                  {formatMoneyCompact(v, { currency })}
                </text>
              </g>
            ))}
            {view.crossesZero && (
              <line
                x1={PAD.l}
                x2={width - PAD.r}
                y1={view.geometry.y("0")}
                y2={view.geometry.y("0")}
                stroke="var(--color-muted-foreground)"
                strokeOpacity={0.5}
              />
            )}
            {view.floor && (
              <g data-testid="chart-floor">
                <line
                  x1={PAD.l}
                  x2={width - PAD.r}
                  y1={view.geometry.y(view.floor)}
                  y2={view.geometry.y(view.floor)}
                  stroke="var(--color-warning)"
                  strokeDasharray="4 4"
                />
                <text
                  x={width - PAD.r}
                  y={view.geometry.y(view.floor) - 5}
                  fontSize={10}
                  textAnchor="end"
                  fill="var(--color-warning)"
                >
                  Floor {formatMoneyCompact(view.floor, { currency })}
                </text>
              </g>
            )}

            {/* The fill is uncovered left to right in step with the line. */}
            <clipPath id={`${gradientId}-reveal`}>
              <rect className="chart-reveal" x={0} y={0} width={width} height={height} />
            </clipPath>
            <path
              className="chart-rise"
              d={view.geometry.area}
              fill={`url(#${gradientId})`}
              clipPath={`url(#${gradientId}-reveal)`}
            />
            <path
              className="chart-draw"
              data-testid="chart-line"
              d={view.geometry.line}
              pathLength={1}
              fill="none"
              stroke="var(--color-primary)"
              strokeWidth={2.25}
              strokeLinecap="round"
              strokeLinejoin="round"
            />

            {[
              [view.lowIndex, owed ? "Most owed" : "Low"],
              [view.highIndex, owed ? "Least owed" : "High"],
            ].map(([index, label]) =>
              typeof index === "number" && index >= 0 && index < view.points.length - 1 ? (
                <circle
                  key={label}
                  className="chart-pop"
                  cx={view.geometry.xy[index][0]}
                  cy={view.geometry.xy[index][1]}
                  r={3.5}
                  fill="var(--color-card)"
                  stroke="var(--color-primary)"
                  strokeWidth={1.75}
                >
                  <title>{`${label} ${money(view.points[index].balance)} · ${formatDate(view.points[index].date)}`}</title>
                </circle>
              ) : null,
            )}

            {/* Today: a dot with a soft pulse once the line arrives. */}
            {(() => {
              const [cx, cy] = view.geometry.xy[view.geometry.xy.length - 1];
              return (
                <g>
                  <circle className="chart-pulse" cx={cx} cy={cy} r={4} fill="var(--color-primary)" />
                  <circle className="chart-pop" cx={cx} cy={cy} r={4} fill="var(--color-primary)" stroke="var(--color-card)" strokeWidth={2} />
                </g>
              );
            })()}

            {hoveredXY && (
              <g pointerEvents="none" data-testid="chart-crosshair">
                <line
                  x1={hoveredXY[0]}
                  x2={hoveredXY[0]}
                  y1={PAD.t - 8}
                  y2={view.geometry.baseline}
                  stroke="var(--color-muted-foreground)"
                  strokeOpacity={0.6}
                  strokeDasharray="3 3"
                />
                <circle cx={hoveredXY[0]} cy={hoveredXY[1]} r={5} fill="var(--color-primary)" stroke="var(--color-card)" strokeWidth={2} />
              </g>
            )}

            <g className="text-muted-foreground" fontSize={10} fill="currentColor">
              <text x={PAD.l} y={height - 8}>
                {axisDate(view.points[0].date, view.spanDays)}
              </text>
              <text x={width / 2} y={height - 8} textAnchor="middle">
                {axisDate(view.points[Math.floor((view.points.length - 1) / 2)].date, view.spanDays)}
              </text>
              <text x={width - PAD.r} y={height - 8} textAnchor="end">
                {axisDate(view.points[view.points.length - 1].date, view.spanDays)}
              </text>
            </g>
          </svg>
        )}
      </div>
      {view && onPickDate && (
        <p className="mt-1 text-xs text-muted-foreground">Click a day to see its transactions.</p>
      )}
    </Card>
  );
}
