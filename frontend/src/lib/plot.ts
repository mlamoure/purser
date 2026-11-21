/* Chart geometry, kept apart from money on purpose (issue #540).
 *
 * An SVG path needs numeric coordinates, so a chart has to turn a decimal
 * string into a number somewhere. This is that one place, and the only
 * thing its result may feed is pixel geometry: the y of a point, the height
 * of a band. It is never formatted, compared for display, summed or sent
 * back to the API — a label next to the chart shows the original decimal
 * string through formatMoney. A float's error here moves a point by a
 * fraction of a pixel and changes no amount anyone reads. */

/** A decimal string as a plot coordinate (0 for null or malformed). */
export function plotCoordinate(value: string | null | undefined): number {
  if (value == null) return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}
