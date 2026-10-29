/* Shared balance-chart primitives (issue #200).
 *
 * Extracted from forecast/main.ts, which had ~120 lines of axis/scale/step
 * drawing inlined. Both balance widgets draw the same picture — a balance
 * line over time with gridlines, an optional dashed reference and marker
 * dots — and differ only in what the markers MEAN (expected events looking
 * forward, actual movements looking back). Sharing the drawing keeps them
 * visually identical without merging two disjoint payload shapes into one
 * widget.
 *
 * Colors come from CHART / CSS variables only (guardrail 9), and nothing
 * here fetches anything (guardrail 8).
 */

import { CHART, linePath, moneyCompact, s, stepPath, ticks } from "./kit";

export const CHART_W = 680;
export const CHART_H = 232;
const PAD_L = 46;
const PAD_R = 16;
const PAD_T = 14;
const PAD_B = 26;

export interface ChartMarker {
  /** x in DOMAIN units (same space as points' x). */
  x: number;
  value: number;
  kind: "low" | "high" | "in" | "out" | "plain";
  /** Optional text drawn near the dot. */
  label?: string;
  /** Hover text. */
  title?: string;
  onClick?: () => void;
}

export interface ChartSpec {
  /** [x, balance] in domain units, ascending by x. */
  points: [number, number][];
  xDomain: [number, number];
  /** Tick label per x position, e.g. a short date. */
  xLabel: (x: number) => string;
  xTicks: number[];
  /** Dashed horizontal reference (an account floor, or zero). */
  reference?: { value: number; label: string; color?: string };
  markers?: ChartMarker[];
  /** Step line (a balance changes discretely) vs a smooth line. */
  step?: boolean;
  /** Extra series drawn faint behind the main one (full-resolution overlay). */
  underlay?: [number, number][];
  /** Dashed continuation drawn after `splitAt` (a forecast tail). */
  splitAt?: number;
  /** A shaded band behind the line (#247): [x, low, high] in domain units,
   * ascending by x — the forecast's pessimistic/optimistic balances. Drawn
   * in the line's shape (stepped when `step`), filled with --chart-band. */
  band?: [number, number, number][];
  ariaLabel: string;
}

export interface ChartScales {
  x: (v: number) => number;
  y: (v: number) => number;
}

const MARKER_COLOR: Record<ChartMarker["kind"], string> = {
  low: CHART.danger,
  high: CHART.positive,
  in: CHART.positive,
  out: CHART.line,
  plain: CHART.line,
};

/** The closed outline of a band: along its highs left to right, back along
 * its lows. Stepped bands hold each value until the next x, as stepPath does. */
export function bandPath(band: [number, number, number][], scales: ChartScales, step: boolean): string {
  const edge = (pick: 1 | 2): [number, number][] => {
    const out: [number, number][] = [];
    band.forEach((row, i) => {
      const px = scales.x(row[0]);
      const py = scales.y(row[pick]);
      if (step && i > 0) out.push([px, scales.y(band[i - 1][pick])]);
      out.push([px, py]);
    });
    return out;
  };
  const outline = [...edge(2), ...edge(1).reverse()];
  return `${outline.map(([px, py], i) => `${i === 0 ? "M" : "L"}${px},${py}`).join(" ")} Z`;
}

/** Draw the chart and hand back the SVG plus its scales, so a caller can
 * position its own overlays (popovers) in the same coordinate space. */
export function balanceChart(spec: ChartSpec): { svg: SVGElement; scales: ChartScales } {
  const values = [
    ...spec.points.map(([, v]) => v),
    ...(spec.band ?? []).flatMap(([, low, high]) => [low, high]),
  ];
  const refValue = spec.reference?.value ?? 0;
  const yMin = Math.min(...values, refValue, 0);
  const yMax = Math.max(...values, refValue, 0);
  const yPad = (yMax - yMin || 1) * 0.08;
  const [x0, x1] = spec.xDomain;
  const span = x1 - x0 || 1;

  const y = (v: number): number =>
    PAD_T + ((yMax + yPad - v) / (yMax + yPad - (yMin - yPad))) * (CHART_H - PAD_T - PAD_B);
  const x = (v: number): number => PAD_L + ((v - x0) / span) * (CHART_W - PAD_L - PAD_R);

  const svg = s("svg", {
    viewBox: `0 0 ${CHART_W} ${CHART_H}`,
    width: "100%",
    role: "img",
    "aria-label": spec.ariaLabel,
  });

  /* gridlines + y labels */
  // Unpadded domain, matching what forecast/main.ts did before the
  // extraction — padding the tick range shifts every gridline.
  for (const tick of ticks(yMin, yMax)) {
    const ty = y(tick);
    svg.append(
      s("line", {
        x1: PAD_L,
        y1: ty,
        x2: CHART_W - PAD_R,
        y2: ty,
        stroke: "var(--border)",
        "stroke-width": 0.5,
      }),
      s(
        "text",
        {
          x: PAD_L - 6,
          y: ty + 4,
          "text-anchor": "end",
          "font-size": 11,
          fill: "var(--text-muted)",
        },
        moneyCompact(tick),
      ),
    );
  }

  /* x labels */
  for (const tick of spec.xTicks) {
    svg.append(
      s(
        "text",
        {
          x: x(tick),
          y: CHART_H - 8,
          "text-anchor": tick === spec.xTicks[0] ? "start" : "middle",
          "font-size": 11,
          fill: "var(--text-muted)",
        },
        spec.xLabel(tick),
      ),
    );
  }

  /* Below-zero shading, so "went negative" reads before any label does.
   * Shaded PER SEGMENT rather than as one full-width band: the band would
   * imply the whole window was negative when only part of it was. */
  const zeroY = y(0);
  let belowStart: number | null = null;
  for (let i = 0; i < spec.points.length; i += 1) {
    const [px, pv] = spec.points[i];
    if (pv < 0 && belowStart === null) belowStart = px;
    const next = spec.points[i + 1]?.[1];
    if (belowStart !== null && (next === undefined || next >= 0)) {
      const endX = spec.points[i + 1]?.[0] ?? x1;
      svg.append(
        s("rect", {
          x: x(belowStart),
          y: zeroY,
          width: Math.max(4, x(endX) - x(belowStart)),
          height: Math.max(4, y(yMin) - zeroY),
          fill: CHART.danger,
          opacity: 0.18,
        }),
      );
      belowStart = null;
    }
  }

  /* the estimate range band, behind everything that follows */
  if (spec.band && spec.band.length > 1) {
    svg.append(
      s("path", {
        "data-role": "band",
        d: bandPath(spec.band, { x, y }, spec.step ?? false),
        fill: "var(--chart-band)",
        stroke: "none",
      }),
    );
  }

  /* full-resolution underlay, faint */
  if (spec.underlay?.length) {
    svg.append(
      s("path", {
        d: linePath(spec.underlay.map(([px, pv]) => [x(px), y(pv)])),
        fill: "none",
        stroke: CHART.line,
        "stroke-width": 1,
        opacity: 0.35,
      }),
    );
  }

  /* dashed reference line */
  if (spec.reference) {
    const ry = y(spec.reference.value);
    const color = spec.reference.color ?? CHART.floor;
    svg.append(
      s("line", {
        x1: PAD_L,
        y1: ry,
        x2: CHART_W - PAD_R,
        y2: ry,
        stroke: color,
        "stroke-width": 1,
        "stroke-dasharray": "5 4",
      }),
      s(
        "text",
        { x: CHART_W - PAD_R - 4, y: ry - 5, "text-anchor": "end", "font-size": 11, fill: color },
        spec.reference.label,
      ),
    );
  }

  /* the line itself, optionally split into solid history + dashed tail */
  const draw = spec.step ? stepPath : linePath;
  const pixel = spec.points.map(([px, pv]) => [x(px), y(pv)] as [number, number]);
  if (spec.splitAt === undefined) {
    svg.append(
      s("path", {
        // Tagged so a caller can insert an async underlay behind it later.
        "data-role": "line",
        d: draw(pixel),
        fill: "none",
        stroke: CHART.line,
        "stroke-width": 2,
        "stroke-linejoin": "round",
      }),
    );
  } else {
    const cut = spec.points.findIndex(([px]) => px > spec.splitAt!);
    const head = cut === -1 ? pixel : pixel.slice(0, cut + 1);
    const tail = cut === -1 ? [] : pixel.slice(Math.max(0, cut - 1));
    svg.append(
      s("path", {
        "data-role": "line",
        d: draw(head),
        fill: "none",
        stroke: CHART.line,
        "stroke-width": 2,
        "stroke-linejoin": "round",
      }),
    );
    if (tail.length > 1) {
      svg.append(
        s("path", {
          d: draw(tail),
          fill: "none",
          stroke: CHART.line,
          "stroke-width": 2,
          "stroke-linejoin": "round",
          "stroke-dasharray": "5 4",
          opacity: 0.75,
        }),
      );
    }
  }

  /* markers */
  for (const marker of spec.markers ?? []) {
    const mx = x(marker.x);
    const my = y(marker.value);
    const color = MARKER_COLOR[marker.kind];
    const dot = s("circle", {
      cx: mx,
      cy: my,
      r: marker.kind === "low" || marker.kind === "high" ? 4.5 : 4,
      fill: color,
      style: marker.onClick ? "cursor:pointer;" : "",
      ...(marker.onClick ? { onclick: marker.onClick } : {}),
    });
    if (marker.title) dot.append(s("title", {}, marker.title));
    svg.append(dot);
    if (marker.label) {
      const below = marker.kind === "low";
      svg.append(
        s(
          "text",
          {
            x: mx,
            y: below ? Math.min(my + 21, CHART_H - PAD_B - 2) : my - 11,
            "text-anchor": "middle",
            "font-size": 11,
            fill: below ? CHART.danger : "var(--text-secondary)",
          },
          marker.label,
        ),
      );
    }
  }

  return { svg, scales: { x, y } };
}
