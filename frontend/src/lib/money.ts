/* Money for display — by string operations only (guardrail 1, issue #540).
 *
 * The API sends money as decimal strings ("-1234.5000", NUMERIC(20,4)). This
 * module never turns one into a JavaScript number: Number()/parseFloat would
 * route the value through a binary float, and toLocaleString on that float
 * rounds 4-place values half-to-even on an inexact binary value. Rounding,
 * grouping and the sign are done on the digit string instead.
 *
 * The only Intl call reads a currency's symbol and minor-unit count from a
 * formatter of the literal 0, never of a monetary value.
 *
 * The MCP widgets share this module (frontend/widgets/lib/kit.ts, #550): Vite
 * inlines it into each self-contained widget bundle (guardrail 8), so it must
 * stay free of imports. */

export type SignDisplay =
  /** "-$5.00" for a negative value, "$5.00" otherwise (the default). */
  | "auto"
  /** "+$5.00" / "-$5.00"; zero gets no sign. For deltas and variances. */
  | "always"
  /** The magnitude: "$5.00" for -5. For a caller whose words carry the sign. */
  | "never";

export type MoneyOptions = {
  /** ISO 4217 code of the value's currency (accounts and schedules carry
   * one). Missing means USD, the ledger's default currency. */
  currency?: string | null;
  signDisplay?: SignDisplay;
  /** Fraction digits to show instead of the currency's minor units (0 for
   * whole units in a compact display), rounded half-up the same way. */
  places?: number;
};

/** A parsed decimal string: sign, integer digits (no leading zeros, at least
 * "0") and fraction digits (possibly empty). */
type Decimal = { negative: boolean; int: string; frac: string };

const DECIMAL = /^([+-])?(\d*)(?:\.(\d*))?$/;

function parse(value: string): Decimal | null {
  const m = DECIMAL.exec(value.trim());
  if (!m || (!m[2] && !m[3])) return null;
  return {
    negative: m[1] === "-",
    int: (m[2] ?? "").replace(/^0+(?=\d)/, "") || "0",
    frac: m[3] ?? "",
  };
}

const isZero = (d: Decimal) => /^0*$/.test(d.int) && /^0*$/.test(d.frac);

/** Add one to a string of digits ("199" -> "200", "99" -> "100"). */
function incrementDigits(digits: string): string {
  const out = digits.split("");
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i] !== "9") {
      out[i] = String.fromCharCode(out[i].charCodeAt(0) + 1);
      return out.join("");
    }
    out[i] = "0";
  }
  return `1${out.join("")}`;
}

/** Round the magnitude to `places` fraction digits, half-up (half away from
 * zero, like Python's ROUND_HALF_UP): "1.005" -> "1.01", "-1.005" -> "-1.01",
 * "1.0049" -> "1.00". Only the first dropped digit decides. */
function roundHalfUp(d: Decimal, places: number): Decimal {
  const frac = d.frac.padEnd(places + 1, "0");
  const kept = d.int + frac.slice(0, places);
  const rounded = frac[places] >= "5" ? incrementDigits(kept) : kept;
  const intLength = rounded.length - places;
  return {
    negative: d.negative,
    int: rounded.slice(0, intLength).replace(/^0+(?=\d)/, "") || "0",
    frac: rounded.slice(intLength),
  };
}

const group = (int: string) => int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

type CurrencyStyle = { symbol: string; places: number };
const styles = new Map<string, CurrencyStyle>();

/** The symbol ("$", "€", "CA$") and minor units (2, or 0 for JPY) of a
 * currency, from Intl applied to the literal 0. An unknown code shows as
 * itself ("XYZ 5.00") with 2 places. */
function currencyStyle(code: string): CurrencyStyle {
  const cached = styles.get(code);
  if (cached) return cached;
  let style: CurrencyStyle;
  try {
    const format = new Intl.NumberFormat("en-US", { style: "currency", currency: code });
    const symbol = format.formatToParts(0).find((p) => p.type === "currency")?.value ?? code;
    const places = format.resolvedOptions().maximumFractionDigits ?? 2;
    // A bare letter code reads better with a space before the digits.
    style = { symbol: /^[A-Z]{3}$/.test(symbol) ? `${symbol} ` : symbol, places };
  } catch {
    style = { symbol: `${code} `, places: 2 };
  }
  styles.set(code, style);
  return style;
}

/** A money string for display: "$1,234.57", "-$380.00", "€12.00".
 *
 * Rounds half-up to the currency's minor units (2 for USD), on the digits.
 * A value that rounds to zero shows no minus sign. null/undefined/"" shows
 * "—"; a string that is not a decimal is returned as is, so a malformed
 * value is visible rather than silently shown as $0.00. */
export function formatMoney(value: string | null | undefined, options: MoneyOptions = {}): string {
  if (value == null || value.trim() === "") return "—";
  const parsed = parse(value);
  if (!parsed) return value;
  const style = currencyStyle((options.currency || "USD").toUpperCase());
  const { symbol } = style;
  const places = options.places ?? style.places;
  const rounded = roundHalfUp(parsed, places);
  const zero = isZero(rounded);
  const digits = group(rounded.int) + (places > 0 ? `.${rounded.frac}` : "");
  const sign = options.signDisplay ?? "auto";
  const prefix =
    sign === "never" || zero ? "" : rounded.negative ? "-" : sign === "always" ? "+" : "";
  return `${prefix}${symbol}${digits}`;
}

/** A real minus sign (U+2212): the widgets' amounts and every estimate. */
export const MINUS_SIGN = "\u2212";

/** The mark an estimated amount carries (#556): U+2248. */
export const ESTIMATE_MARK = "\u2248";

/** Formatted money with its leading hyphen as a real minus sign:
 * "-$142.00" -> "−$142.00". */
export function typographicMinus(text: string): string {
  return text.startsWith("-") ? `${MINUS_SIGN}${text.slice(1)}` : text;
}

/** An estimated amount as text (#556): "≈ −$95.00" — the mark, a space, then
 * the amount as formatMoney renders it with a real minus sign. For text-only
 * places (a tooltip, a chart label); on the page, <EstimatedAmount> shows the
 * same and tells a screen reader "estimated", where the bare mark reads
 * "approximately equal to". A missing amount is "—", unmarked. */
export function formatEstimate(value: string | null | undefined, options: MoneyOptions = {}): string {
  const text = formatMoney(value, options);
  return text === "—" ? text : `${ESTIMATE_MARK} ${typographicMinus(text)}`;
}

/** -1, 0 or 1 by the decimal string's sign, without parsing it into a
 * number. null/undefined/malformed is 0. */
export function moneySign(value: string | null | undefined): -1 | 0 | 1 {
  const d = value == null ? null : parse(value);
  if (!d || isZero(d)) return 0;
  return d.negative ? -1 : 1;
}

/** The decimal string with its sign flipped ("380.00" -> "-380.00"). */
export function negateMoney(value: string): string {
  const d = parse(value);
  if (!d) return value;
  if (isZero(d)) return value.trim().replace(/^[+-]/, "");
  const body = d.int + (d.frac ? `.${d.frac}` : "");
  return d.negative ? body : `-${body}`;
}

/** Compare two decimal strings exactly: negative when a < b, 0 when equal,
 * positive when a > b. Malformed strings compare as zero. */
export function compareMoney(a: string, b: string): number {
  const x = parse(a) ?? { negative: false, int: "0", frac: "" };
  const y = parse(b) ?? { negative: false, int: "0", frac: "" };
  const sx = isZero(x) ? 0 : x.negative ? -1 : 1;
  const sy = isZero(y) ? 0 : y.negative ? -1 : 1;
  if (sx !== sy) return sx - sy;
  if (sx === 0) return 0;
  // Same sign: compare magnitudes digit by digit, then flip for negatives.
  const width = Math.max(x.frac.length, y.frac.length);
  const mx = x.int.replace(/^0+/, "") + x.frac.padEnd(width, "0");
  const my = y.int.replace(/^0+/, "") + y.frac.padEnd(width, "0");
  const magnitude =
    mx.length !== my.length ? mx.length - my.length : mx < my ? -1 : mx > my ? 1 : 0;
  return sx * Math.sign(magnitude);
}

/** A stored amount ("500.0000") as an editable string ("500.00"): trailing
 * zeros beyond the cents dropped, never rounded, so editing and saving an
 * unchanged value sends back the same number. */
export function moneyEditText(stored: string): string {
  const m = /^(-?\d+)(?:\.(\d*))?$/.exec(stored.trim());
  if (!m) return stored;
  const places = (m[2] ?? "").replace(/0+$/, "").padEnd(2, "0");
  return `${m[1]}.${places}`;
}

/** A short money label for a chart axis (#594): "$950", "$12.3k", "-$1.25M",
 * "$4B". Like formatMoney it works on the digit string — the scale shift is a
 * move of the decimal point — so the label is exact to its rounding. Values
 * under 1,000 show whole currency units; larger ones keep up to one decimal
 * (two in millions and up) with trailing zeros dropped. */
export function formatMoneyCompact(value: string | null | undefined, options: MoneyOptions = {}): string {
  if (value == null || value.trim() === "") return "—";
  const parsed = parse(value);
  if (!parsed) return value;
  const { symbol } = currencyStyle((options.currency || "USD").toUpperCase());
  const scales: [number, string, number][] = [
    [9, "B", 2],
    [6, "M", 2],
    [3, "k", 1],
  ];
  const digits = parsed.int.replace(/^0+(?=\d)/, "");
  const shift = ([power, unit, places]: [number, string, number]): [Decimal, string] => [
    roundHalfUp(
      {
        negative: parsed.negative,
        int: digits.slice(0, digits.length - power) || "0",
        frac: digits.slice(Math.max(0, digits.length - power)).padStart(power, "0") + parsed.frac,
      },
      places,
    ),
    unit,
  ];
  const at = scales.findIndex(([power]) => digits.length > power);
  let [shown, suffix] = at >= 0 ? shift(scales[at]) : [roundHalfUp(parsed, 0), ""];
  // 999,999 rounds to "1,000k": say "1M" instead (and 999.6 -> "$1k").
  const bigger = at >= 0 ? at - 1 : scales.length - 1;
  if (shown.int.length > 3 && bigger >= 0) [shown, suffix] = shift(scales[bigger]);
  const frac = shown.frac.replace(/0+$/, "");
  const zero = isZero(shown);
  const prefix = zero || !shown.negative ? "" : "-";
  return `${prefix}${symbol}${group(shown.int)}${frac ? `.${frac}` : ""}${suffix}`;
}

/** The exact sum of decimal strings ("12.50" + "-0.0001" -> "12.4999"), for
 * subtotals the API does not send (#593). Each value is scaled to an integer
 * at the widest fraction among them and added as a BigInt, so no amount ever
 * passes through a binary float. The result keeps that widest fraction;
 * an empty list sums to "0". Malformed strings count as zero. */
export function sumMoney(values: readonly (string | null | undefined)[]): string {
  const parsed = values.map((v) => (v == null ? null : parse(v))).filter((d): d is Decimal => d !== null);
  const places = Math.max(0, ...parsed.map((d) => d.frac.length));
  let total = 0n;
  for (const d of parsed) {
    const scaled = BigInt(d.int + d.frac.padEnd(places, "0"));
    total += d.negative ? -scaled : scaled;
  }
  const negative = total < 0n;
  const digits = (negative ? -total : total).toString().padStart(places + 1, "0");
  const int = digits.slice(0, digits.length - places);
  const frac = digits.slice(digits.length - places);
  return `${negative ? "-" : ""}${int}${places > 0 ? `.${frac}` : ""}`;
}
