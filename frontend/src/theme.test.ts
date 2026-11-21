/// <reference types="vite/client" />
/* Theme tokens (#543): the SPA paints only through the tokens in index.css,
 * each token has a dark value, and the text tokens keep WCAG AA contrast
 * (4.5:1) on the surfaces they sit on in both schemes. */

import { describe, expect, it } from "vitest";
import css from "./index.css?raw";

type Oklch = [number, number, number];

/** `--color-x: oklch(L C H);` declarations inside the first `{...}` after `start`. */
function tokens(source: string, start: string): Record<string, Oklch> {
  const from = source.indexOf(start);
  expect(from, `${start} block`).toBeGreaterThanOrEqual(0);
  const body = source.slice(from, source.indexOf("}", from));
  const out: Record<string, Oklch> = {};
  for (const m of body.matchAll(/--color-([\w-]+):\s*oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)/g)) {
    out[m[1]] = [Number(m[2]), Number(m[3]), Number(m[4])];
  }
  return out;
}

const light = tokens(css, "@theme {");
const dark = tokens(css, "@media (prefers-color-scheme: dark)");

/** Relative luminance (WCAG) of an oklch color, via linear sRGB. */
function luminance([L, C, h]: Oklch): number {
  const a = C * Math.cos((h * Math.PI) / 180);
  const b = C * Math.sin((h * Math.PI) / 180);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const clamp = (x: number) => Math.min(1, Math.max(0, x));
  const r = clamp(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s);
  const g = clamp(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s);
  const bl = clamp(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s);
  return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
}

function contrast(fg: Oklch, bg: Oklch): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// Text token -> the surfaces it is drawn on.
const SURFACES = ["background", "card", "muted"];
const TEXT_PAIRS: [string, string][] = [
  ...["foreground", "muted-foreground", "primary", "destructive", "success", "warning"].flatMap(
    (fg) => SURFACES.map((bg): [string, string] => [fg, bg]),
  ),
  ["primary-foreground", "primary"],
  ["destructive-foreground", "destructive"],
  // Badges: <Badge variant="green|amber|red">.
  ["success", "success-soft"],
  ["warning", "warning-soft"],
  ["destructive", "destructive-soft"],
];

describe("theme tokens", () => {
  it("gives every light token a dark value, and no extras", () => {
    const colors = Object.keys(light).sort();
    expect(colors.length).toBeGreaterThan(10);
    expect(Object.keys(dark).sort()).toEqual(colors);
  });

  it("lets native controls follow the scheme", () => {
    expect(css).toMatch(/color-scheme:\s*light dark/);
  });

  it.each([
    ["light", light],
    ["dark", dark],
  ])("keeps text at WCAG AA contrast in the %s scheme", (_, theme) => {
    const failing = TEXT_PAIRS.map(([fg, bg]) => ({ pair: `${fg} on ${bg}`, ratio: contrast(theme[fg], theme[bg]) }))
      .filter(({ ratio }) => ratio < 4.5)
      .map(({ pair, ratio }) => `${pair}: ${ratio.toFixed(2)}`);
    expect(failing).toEqual([]);
  });
});

describe("components", () => {
  // Every non-test source file, as text.
  const sources = import.meta.glob(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./test/**", "!./api/**"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("color only through theme tokens", () => {
    // A literal palette utility (text-red-700, bg-emerald-50, ...), a `dark:`
    // variant or a hex color would skip the dark scheme or fork it.
    const literal =
      /\b(?:[a-z]+:)?(?:text|bg|border|ring|fill|stroke|outline|divide|decoration|from|to|via|accent|caret|shadow)-(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|gray|zinc|neutral|stone|white|black)\b|\bdark:|["'`]#[0-9a-fA-F]{3,8}["'`]/;
    const offenders = Object.entries(sources).flatMap(([file, text]) =>
      text
        .split("\n")
        .map((line, i) => [line, i + 1] as const)
        .filter(([line]) => literal.test(line))
        .map(([line, n]) => `${file}:${n}: ${line.trim()}`),
    );
    expect(Object.keys(sources).length).toBeGreaterThan(20);
    expect(offenders).toEqual([]);
  });
});
