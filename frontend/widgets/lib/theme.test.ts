/// <reference types="vite/client" />
/* Widget theme contrast (#556, guardrail 9): every text color in
 * theme.css keeps WCAG AA (4.5:1) on each surface a widget paints, and on
 * its own tinted background, in both schemes; the host-announced
 * [data-theme] blocks repeat the OS-preference ones exactly. */

import { describe, expect, it } from "vitest";
import css from "./theme.css?raw";

type Tokens = Record<string, string>;

/** `--name: #rrggbb;` declarations in the first `{...}` after `start`. */
function block(start: string): Tokens {
  const from = css.indexOf(start);
  expect(from, start).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", from + start.length - 1);
  const body = css.slice(open + 1, css.indexOf("}", open));
  return Object.fromEntries(
    [...body.matchAll(/(--[\w-]+):\s*(#[0-9a-f]{6})\s*;/gi)].map((m) => [m[1], m[2].toLowerCase()]),
  );
}

/** WCAG 2 relative luminance of an sRGB hex color. */
function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const LIGHT = block(":root {");
const DARK = block("@media (prefers-color-scheme: dark) {\n  :root {");
const TEXT = ["primary", "secondary", "muted", "accent", "danger", "warning", "success"];
const SURFACES = ["--surface-2", "--surface-1", "--page"];
const TINTED = ["accent", "danger", "warning", "success"];

describe("widget theme", () => {
  it("repeats each scheme exactly when the host announces it", () => {
    expect(block(':root[data-theme="light"] {')).toEqual(LIGHT);
    expect(block(':root[data-theme="dark"] {')).toEqual(DARK);
  });

  for (const [scheme, tokens] of [
    ["light", LIGHT],
    ["dark", DARK],
  ] as const) {
    it(`keeps every text color at 4.5:1 on every surface (${scheme})`, () => {
      for (const name of TEXT) {
        for (const surface of SURFACES) {
          const ratio = contrast(tokens[`--text-${name}`], tokens[surface]);
          expect(ratio, `--text-${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    });

    it(`keeps tinted badges and banners at 4.5:1 (${scheme})`, () => {
      for (const name of TINTED) {
        const ratio = contrast(tokens[`--text-${name}`], tokens[`--bg-${name}`]);
        expect(ratio, `--text-${name} on --bg-${name}`).toBeGreaterThanOrEqual(4.5);
      }
    });
  }
});
