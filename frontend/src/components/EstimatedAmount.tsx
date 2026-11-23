/* An estimated amount on the page (#556): "≈ −$95.00" — the mark, a space,
 * then the amount as formatMoney renders it with a real minus sign. The
 * scheduled transactions, the forecast, the overdue bills and the suggestion
 * headlines share it. The mark is hidden from screen readers and a visually
 * hidden "estimated" stands in for it, so assistive technology hears
 * "estimated −$95.00", never "approximately equal to". Text-only places (a
 * tooltip, a chart label) use formatEstimate from lib/money instead. */

import { Fragment } from "react";
import { ESTIMATE_MARK, formatMoney, typographicMinus, type MoneyOptions } from "../lib/money";

/** The "≈" an estimate starts with, read as "estimated". The space after it
 * belongs to the text that follows, so both readings keep one. */
function EstimateMark() {
  return (
    <>
      <span aria-hidden="true">{ESTIMATE_MARK}</span>
      <span className="sr-only">estimated</span>
    </>
  );
}

/** An estimated amount: "≈ −$95.00", heard as "estimated −$95.00". A missing
 * amount shows "—", unmarked, like formatMoney. */
export function EstimatedAmount({ value, ...options }: { value: string | null | undefined } & MoneyOptions) {
  const text = formatMoney(value, options);
  if (text === "—") return <>{text}</>;
  return (
    <span>
      <EstimateMark />
      {` ${typographicMinus(text)}`}
    </span>
  );
}

/** Server-composed text (a suggestion headline, which marks an estimated
 * amount "≈ −95.00" the same way) with each mark read as "estimated". */
export function EstimateText({ text }: { text: string }) {
  const parts = text.split(`${ESTIMATE_MARK} `);
  if (parts.length === 1) return <>{text}</>;
  return (
    <>
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 && <EstimateMark />}
          {i > 0 ? ` ${part}` : part}
        </Fragment>
      ))}
    </>
  );
}
