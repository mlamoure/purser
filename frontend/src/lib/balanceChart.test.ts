/* Balance chart geometry (#594). Money arrives as decimal strings; only the
 * pixel output is numeric. */

import { describe, expect, it } from "vitest";
import { chartGeometry, dayNumber, isLiabilityType, monotonePath, nearestIndex, shownValue } from "./balanceChart";

const box = { width: 400, height: 200, pad: { t: 10, r: 10, b: 10, l: 10 } };

/** Every coordinate pair in an SVG path string. */
const coords = (d: string) => [...d.matchAll(/(-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])]);

describe("monotonePath", () => {
  it("handles the trivial cases", () => {
    expect(monotonePath([])).toBe("");
    expect(monotonePath([[1, 2]])).toBe("M1,2");
    expect(monotonePath([[0, 0], [10, 5]])).toBe("M0,0L10,5");
  });

  it("never overshoots: a flat run stays flat and a step stays inside its neighbours", () => {
    const d = monotonePath([[0, 50], [10, 50], [20, 10], [30, 10]]);
    for (const [, y] of coords(d)) {
      expect(y).toBeGreaterThanOrEqual(10);
      expect(y).toBeLessThanOrEqual(50);
    }
    // The first segment's control points sit on the flat line.
    expect(d.startsWith("M0,50C3.3,50 6.7,50 10,50")).toBe(true);
  });
});

describe("chartGeometry", () => {
  const points = [
    { date: "2026-01-01", balance: "100.0000" },
    { date: "2026-01-02", balance: "300.0000" },
    { date: "2026-01-11", balance: "200.0000" },
  ];

  it("spaces x by calendar day and picks extremes by exact comparison", () => {
    const g = chartGeometry(points, box);
    expect(g.lo).toBe("100.0000");
    expect(g.hi).toBe("300.0000");
    const [x0, x1, x2] = g.xy.map(([x]) => x);
    expect(x0).toBe(10);
    expect(x2).toBe(390);
    expect(x1 - x0).toBeCloseTo((x2 - x0) / 10);
    // Higher balances sit higher (smaller y), inside the padding.
    expect(g.xy[1][1]).toBeLessThan(g.xy[0][1]);
    for (const [, y] of g.xy) {
      expect(y).toBeGreaterThan(10);
      expect(y).toBeLessThan(190);
    }
    expect(g.area.endsWith(`L390.0,190L10.0,190Z`)).toBe(true);
  });

  it("widens the range to keep a reference line in view", () => {
    const g = chartGeometry(points, box, ["-50.00"]);
    expect(g.lo).toBe("-50.00");
    expect(g.y("-50.00")).toBeGreaterThan(g.xy[0][1]);
  });

  it("centres a flat series", () => {
    const g = chartGeometry(
      [
        { date: "2026-01-01", balance: "0" },
        { date: "2026-01-02", balance: "0" },
      ],
      box,
    );
    expect(g.xy[0][1]).toBeCloseTo(100);
  });
});

describe("helpers", () => {
  it("nearestIndex finds the closest x", () => {
    const xy: [number, number][] = [[0, 0], [10, 0], [30, 0]];
    expect(nearestIndex(xy, -5)).toBe(0);
    expect(nearestIndex(xy, 14)).toBe(1);
    expect(nearestIndex(xy, 21)).toBe(2);
    expect(nearestIndex(xy, 99)).toBe(2);
    expect(nearestIndex([], 5)).toBe(-1);
  });

  it("dayNumber counts whole days", () => {
    expect(dayNumber("2026-03-01") - dayNumber("2026-02-01")).toBe(28);
  });

  it("charts a liability as what is owed", () => {
    expect(isLiabilityType("loan")).toBe(true);
    expect(isLiabilityType("credit_card")).toBe(true);
    expect(isLiabilityType("checking")).toBe(false);
    expect(shownValue("-380.0000", true)).toBe("380.0000");
    expect(shownValue("-380.0000", false)).toBe("-380.0000");
  });
});
