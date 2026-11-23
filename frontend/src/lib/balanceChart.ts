/* Geometry for the account page's balance chart (#594).
 *
 * Money stays decimal strings here: the extremes are picked by exact string
 * comparison and every label shows an original string. Only pixel positions
 * go through plotCoordinate (lib/plot.ts), the one place a decimal string
 * may become a number. */

import { compareMoney, negateMoney } from "./money";
import { plotCoordinate } from "./plot";

export type ChartPoint = { date: string; balance: string };

export type ChartBox = { width: number; height: number; pad: { t: number; r: number; b: number; l: number } };

export type ChartGeometry = {
  /** Each point's pixel position, in input order. */
  xy: [number, number][];
  /** The line, a monotone curve through every point. */
  line: string;
  /** The line closed down to the plot's bottom edge, for the gradient fill. */
  area: string;
  /** Lowest and highest plotted strings (input values plus any reference). */
  lo: string;
  hi: string;
  /** Pixel y of a decimal string on this scale. */
  y: (value: string) => number;
  /** Bottom edge of the plot area. */
  baseline: number;
};

/** Days since the epoch for an ISO date, so x spacing follows the calendar
 * even where the sampled dates are not evenly spaced (the forced start). */
export function dayNumber(isoDate: string): number {
  const [y, m, d] = isoDate.split("-").map(Number);
  return Date.UTC(y, (m ?? 1) - 1, d ?? 1) / 86_400_000;
}

/** A liability charts what is owed, not the ledger's negative balance (#594):
 * a mortgage paid down reads as a line falling toward zero. */
export const isLiabilityType = (type: string | null | undefined) =>
  type === "loan" || type === "credit_card";

/** The value a point plots: the ledger balance, or what is owed. */
export const shownValue = (balance: string, owed: boolean) =>
  owed ? negateMoney(balance) : balance;

/** A smooth path through `pts` that never overshoots (Fritsch–Carlson
 * monotone cubic): between two equal balances the curve stays flat, so a
 * balance that changed on one day still reads as changing on that day. */
export function monotonePath(pts: [number, number][]): string {
  const n = pts.length;
  if (n === 0) return "";
  const f = (v: number) => (Math.round(v * 10) / 10).toString();
  if (n === 1) return `M${f(pts[0][0])},${f(pts[0][1])}`;
  if (n === 2) return `M${f(pts[0][0])},${f(pts[0][1])}L${f(pts[1][0])},${f(pts[1][1])}`;
  const dx: number[] = [];
  const slope: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1][0] - pts[i][0] || 1e-9);
    slope.push((pts[i + 1][1] - pts[i][1]) / dx[i]);
  }
  const tangent: number[] = [slope[0]];
  for (let i = 1; i < n - 1; i++) {
    if (slope[i - 1] * slope[i] <= 0) tangent.push(0);
    else {
      const w1 = 2 * dx[i] + dx[i - 1];
      const w2 = dx[i] + 2 * dx[i - 1];
      tangent.push((w1 + w2) / (w1 / slope[i - 1] + w2 / slope[i]));
    }
  }
  tangent.push(slope[n - 2]);
  let d = `M${f(pts[0][0])},${f(pts[0][1])}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    const h = dx[i] / 3;
    d += `C${f(x0 + h)},${f(y0 + tangent[i] * h)} ${f(x1 - h)},${f(y1 - tangent[i + 1] * h)} ${f(x1)},${f(y1)}`;
  }
  return d;
}

/** Scales and paths for `points` (already in the plotted sign) inside `box`.
 * `references` (a floor, zero) widen the value range so they stay visible. */
export function chartGeometry(points: ChartPoint[], box: ChartBox, references: string[] = []): ChartGeometry {
  const values = [...points.map((p) => p.balance), ...references];
  const lo = values.reduce((a, b) => (compareMoney(b, a) < 0 ? b : a), values[0] ?? "0");
  const hi = values.reduce((a, b) => (compareMoney(b, a) > 0 ? b : a), values[0] ?? "0");
  const { width, height, pad } = box;
  const innerH = height - pad.t - pad.b;
  const loY = plotCoordinate(lo);
  const hiY = plotCoordinate(hi);
  // A flat series sits mid-height rather than on an edge; otherwise 8% of
  // headroom above and below keeps the line off the frame.
  const span = hiY - loY;
  const margin = span === 0 ? Math.max(Math.abs(hiY) * 0.1, 1) : span * 0.08;
  const vMin = loY - margin;
  const vMax = hiY + margin;
  const y = (value: string) => pad.t + (1 - (plotCoordinate(value) - vMin) / (vMax - vMin)) * innerH;

  const days = points.map((p) => dayNumber(p.date));
  const first = days[0] ?? 0;
  const last = days[days.length - 1] ?? first;
  const innerW = width - pad.l - pad.r;
  const x = (day: number) => pad.l + (last === first ? innerW : ((day - first) / (last - first)) * innerW);
  const xy = points.map((p, i): [number, number] => [x(days[i]), y(p.balance)]);
  const line = monotonePath(xy);
  const baseline = height - pad.b;
  const area =
    xy.length > 1
      ? `${line}L${xy[xy.length - 1][0].toFixed(1)},${baseline}L${xy[0][0].toFixed(1)},${baseline}Z`
      : "";
  return { xy, line, area, lo, hi, y, baseline };
}

/** The index of the point nearest pixel `px` horizontally (binary search;
 * `xy` is ascending in x). */
export function nearestIndex(xy: [number, number][], px: number): number {
  if (xy.length === 0) return -1;
  let lo = 0;
  let hi = xy.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xy[mid][0] < px) lo = mid;
    else hi = mid;
  }
  return Math.abs(xy[lo][0] - px) <= Math.abs(xy[hi][0] - px) ? lo : hi;
}
