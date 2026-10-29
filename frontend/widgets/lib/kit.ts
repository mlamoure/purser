/* The shared widget kit (issue #72): DOM/SVG builders, money and date
 * formatting, scales, and chart path helpers. Pure functions emitting DOM —
 * no framework, so every bundle stays a few self-contained kilobytes.
 *
 * Chart mark colors are fixed hexes chosen to read on both themes (the
 * mockups do the same); text/surface colors come from theme.css vars.
 */

export const CHART = {
  line: "#378ADD",
  danger: "#E24B4A",
  positive: "#1D9E75",
  positiveFill: "#5DCAA5",
  floor: "#BA7517",
  liabilities: "#D85A30",
  bar: "#7F77DD",
} as const;

type Attrs = Record<string, string | number | boolean | ((ev: Event) => void)>;

function assign(node: Element, attrs: Attrs): void {
  for (const [key, value] of Object.entries(attrs)) {
    if (typeof value === "function") {
      node.addEventListener(key.replace(/^on/, ""), value as EventListener);
    } else if (key === "style" && typeof value === "string") {
      node.setAttribute("style", value);
    } else if (value === false) {
      continue;
    } else {
      node.setAttribute(key, String(value));
    }
  }
}

/** HTML element builder: h("div", {class: "card"}, child, "text"). */
export function h(tag: string, attrs: Attrs = {}, ...children: (Node | string | null)[]): HTMLElement {
  const node = document.createElement(tag);
  assign(node, attrs);
  for (const child of children) {
    if (child === null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** SVG element builder (correct namespace). */
export function s(tag: string, attrs: Attrs = {}, ...children: (Node | string | null)[]): SVGElement {
  const node = document.createElementNS(SVG_NS, tag);
  assign(node, attrs);
  for (const child of children) {
    if (child === null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

export function clear(node: Element): void {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/* ----------------------------------------------------------------------- */
/* Formatting. Amounts arrive as decimal strings (guardrail 1) and pass    */
/* through unchanged; Number() is used only to lay out pixels and to       */
/* format for display.                                                     */
/* ----------------------------------------------------------------------- */

export function num(dec: string): number {
  return Number(dec);
}

/** "-142.00" -> "−$142" · "17.99" -> "$17.99" · sign: always show +/−.
 * A missing value is an amount the user's privacy settings withheld from
 * the agent (#437): it reads "withheld", never "$NaN". */
export function money(
  dec: string | null | undefined,
  opts: { sign?: boolean; cents?: boolean } = {},
): string {
  if (dec == null) return "withheld";
  const value = Number(dec);
  const abs = Math.abs(value);
  const wantCents = opts.cents ?? (abs < 1000 && abs % 1 !== 0);
  const body = abs.toLocaleString("en-US", {
    minimumFractionDigits: wantCents ? 2 : 0,
    maximumFractionDigits: wantCents ? 2 : 0,
  });
  const sign = value < 0 ? "−" : opts.sign ? "+" : "";
  return `${sign}$${body}`;
}

/** Axis-compact: 2500 -> "$2.5k", 450000 -> "$450k", 1200000 -> "$1.2M". */
export function moneyCompact(value: number): string {
  const sign = value < 0 ? "−" : "";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${sign}$${trimZero((abs / 1_000_000).toFixed(1))}M`;
  if (abs >= 1_000) return `${sign}$${trimZero((abs / 1_000).toFixed(1))}k`;
  return `${sign}$${Math.round(abs)}`;
}

function trimZero(fixed: string): string {
  return fixed.endsWith(".0") ? fixed.slice(0, -2) : fixed;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-07-19" -> "Jul 19" (no timezone surprises: parse the parts). */
export function shortDate(iso: string): string {
  const [, month, day] = iso.split("-").map((p) => parseInt(p, 10));
  return `${MONTHS[(month ?? 1) - 1]} ${day}`;
}

/** "2026-07-19" -> "Jul '26". */
export function monthYear(iso: string): string {
  const [year, month] = iso.split("-").map((p) => parseInt(p, 10));
  return `${MONTHS[(month ?? 1) - 1]} '${String(year).slice(2)}`;
}

/** Whole days from ISO date a to b. */
export function dayDiff(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

export function addDays(iso: string, days: number): string {
  const t = new Date(Date.parse(iso) + days * 86_400_000);
  return t.toISOString().slice(0, 10);
}

/** Last ltree segment, capitalized: "assets.checking" -> "Checking". */
export function pathLabel(path: string): string {
  const leaf = path.split(".").at(-1) ?? path;
  return leaf.charAt(0).toUpperCase() + leaf.slice(1).replace(/_/g, " ");
}

/* ----------------------------------------------------------------------- */
/* Scales & paths                                                          */
/* ----------------------------------------------------------------------- */

export type Scale = (v: number) => number;

export function linear(d0: number, d1: number, r0: number, r1: number): Scale {
  const span = d1 - d0 || 1;
  return (v: number) => r0 + ((v - d0) / span) * (r1 - r0);
}

/** A few round ticks covering [min, max]. */
export function ticks(min: number, max: number, count = 3): number[] {
  const span = max - min || 1;
  const step = niceStep(span / Math.max(1, count - 1));
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + step * 0.01; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

function niceStep(raw: number): number {
  const mag = 10 ** Math.floor(Math.log10(Math.abs(raw) || 1));
  const norm = raw / mag;
  const nice = norm >= 7.5 ? 10 : norm >= 3 ? 5 : norm >= 1.5 ? 2 : 1;
  return nice * mag;
}

/** Step-after path through [x, y] points (every vertical move is an event). */
export function stepPath(points: [number, number][]): string {
  if (points.length === 0) return "";
  const [first, ...rest] = points;
  let d = `M${first[0]},${first[1]}`;
  let prevY = first[1];
  for (const [x, y] of rest) {
    d += ` L${x},${prevY} L${x},${y}`;
    prevY = y;
  }
  return d;
}

/** Straight line path through [x, y] points. */
export function linePath(points: [number, number][]): string {
  return points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x},${y}`).join(" ");
}

/** Close a line path down to the baseline for an area fill. */
export function areaPath(points: [number, number][], baselineY: number): string {
  if (points.length === 0) return "";
  const line = linePath(points);
  const last = points[points.length - 1];
  const first = points[0];
  return `${line} L${last[0]},${baselineY} L${first[0]},${baselineY} Z`;
}

/* ----------------------------------------------------------------------- */
/* Inline SVG icons (mockups used a CDN icon font; bundles must not).      */
/* ----------------------------------------------------------------------- */

const ICON_PATHS: Record<string, string> = {
  alert: "M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
  receipt: "M5 21V5a2 2 0 012-2h10a2 2 0 012 2v16l-3-2-2 2-2-2-2 2-2-2-3 2M9 7h6m-6 4h6",
  check: "M5 12l5 5L20 7",
  "arrow-up": "M7 17L17 7M17 7H8m9 0v9",
  clock: "M12 7v5l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z",
  "trend-up": "M3 17l6-6 4 4 8-8m0 0h-5m5 0v5",
};

export function icon(name: keyof typeof ICON_PATHS, color: string, size = 17): SVGElement {
  return s(
    "svg",
    {
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: color,
      "stroke-width": 1.7,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    s("path", { d: ICON_PATHS[name] ?? "" }),
  );
}
