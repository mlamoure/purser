/* The estimate range band on the shared balance chart (#247, decision 85):
 * drawn behind the line in its shape, themed only through --chart-band
 * (guardrail 9), and inside the scales even where it reaches past the line. */

import { describe, expect, it } from "vitest";
import { balanceChart, bandPath } from "./chart";

const base = {
  points: [
    [0, 100],
    [5, 40],
    [10, 40],
  ] as [number, number][],
  xDomain: [0, 10] as [number, number],
  xTicks: [0, 5, 10],
  xLabel: (x: number) => String(x),
  step: true,
  ariaLabel: "test chart",
};

describe("balanceChart band", () => {
  it("draws no band unless asked", () => {
    const { svg } = balanceChart(base);
    expect(svg.querySelector('[data-role="band"]')).toBeNull();
  });

  it("fills the band with the theme token, behind the line", () => {
    const { svg } = balanceChart({
      ...base,
      band: [
        [0, 100, 100],
        [5, -20, 70],
        [10, -20, 70],
      ],
    });
    const band = svg.querySelector('[data-role="band"]');
    expect(band).not.toBeNull();
    expect(band!.getAttribute("fill")).toBe("var(--chart-band)");
    const children = [...svg.children];
    expect(children.indexOf(band!)).toBeLessThan(children.indexOf(svg.querySelector('[data-role="line"]')!));
  });

  it("widens the scale to the band's low", () => {
    const plain = balanceChart(base).scales;
    const banded = balanceChart({ ...base, band: [[0, 100, 100], [5, -500, 70], [10, -500, 70]] }).scales;
    // The same balance sits higher on a chart whose domain reaches -500.
    expect(banded.y(40)).toBeLessThan(plain.y(40));
  });
});

describe("bandPath", () => {
  const scales = { x: (v: number) => v * 10, y: (v: number) => -v };

  it("walks the highs forward and the lows back, stepped like the line", () => {
    const d = bandPath(
      [
        [0, 1, 3],
        [1, 0, 4],
      ],
      scales,
      true,
    );
    expect(d).toBe("M0,-3 L10,-3 L10,-4 L10,0 L10,-1 L0,-1 Z");
  });

  it("joins straight when the line is not stepped", () => {
    expect(
      bandPath(
        [
          [0, 1, 3],
          [1, 0, 4],
        ],
        scales,
        false,
      ),
    ).toBe("M0,-3 L10,-4 L10,0 L0,-1 Z");
  });
});
