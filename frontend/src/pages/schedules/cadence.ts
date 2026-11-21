/** Cadence helpers for scheduled transactions (#236): the picker's RRULE
 * shapes and the human label the table shows. */

export const selectClass =
  "h-9 rounded-md border border-input bg-background px-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

export const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
export const WEEKDAY_LABELS: Record<string, string> = {
  MO: "Monday",
  TU: "Tuesday",
  WE: "Wednesday",
  TH: "Thursday",
  FR: "Friday",
  SA: "Saturday",
  SU: "Sunday",
};

export type Cadence = "weekly" | "biweekly" | "semimonthly" | "monthly" | "quarterly" | "yearly" | "custom";

/** Cadence picker state → RRULE. The picker covers the shapes detection
 * proposes; anything else is a raw RRULE. */
export function buildRrule(c: {
  cadence: Cadence;
  weekday: string;
  day: number;
  day2: number;
  month: number;
  custom: string;
}): string {
  const day = Math.min(Math.max(c.day, 1), 28);
  const day2 = Math.min(Math.max(c.day2, 1), 28);
  switch (c.cadence) {
    case "weekly":
      return `FREQ=WEEKLY;BYDAY=${c.weekday}`;
    case "biweekly":
      return `FREQ=WEEKLY;INTERVAL=2;BYDAY=${c.weekday}`;
    case "semimonthly":
      return `FREQ=MONTHLY;BYMONTHDAY=${Math.min(day, day2)},${Math.max(day, day2)}`;
    case "monthly":
      return `FREQ=MONTHLY;BYMONTHDAY=${day}`;
    case "quarterly":
      return `FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=${day}`;
    case "yearly":
      return `FREQ=YEARLY;BYMONTH=${c.month};BYMONTHDAY=${day}`;
    default:
      return c.custom.trim();
  }
}

export function rruleParams(rrule: string): Record<string, string> {
  /* Only the RRULE line: a rule may carry EXDATE/RDATE lines after it
   * (agent-authored schedules do), which are not key=value parameters. */
  const line = rrule.split(/\r?\n/).find((l) => /^(RRULE:)?FREQ=/i.test(l.trim())) ?? rrule;
  const out: Record<string, string> = {};
  for (const part of line.trim().replace(/^RRULE:/i, "").split(";")) {
    const [k, v] = part.split("=");
    if (k && v) out[k.toUpperCase()] = v.trim();
  }
  return out;
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Human cadence for the table, mirroring core_forecasting.cadence_label
 * plus the day-of-month detail the RRULE carries. */
export function cadenceLabel(rrule: string): string {
  const p = rruleParams(rrule);
  const interval = Number(p.INTERVAL ?? "1");
  const days = p.BYMONTHDAY ? ` on the ${p.BYMONTHDAY.split(",").join(" & ")}` : "";
  const weekday = p.BYDAY ? ` on ${WEEKDAY_LABELS[p.BYDAY] ?? p.BYDAY}` : "";
  switch (p.FREQ) {
    case "WEEKLY":
      return (interval === 1 ? "weekly" : interval === 2 ? "every 2 weeks" : `every ${interval} weeks`) + weekday;
    case "MONTHLY":
      if (interval === 1) return (p.BYMONTHDAY?.includes(",") ? "semi-monthly" : "monthly") + days;
      return `every ${interval} months${days}`;
    case "YEARLY":
      return `yearly${p.BYMONTH ? ` in ${MONTH_NAMES[Number(p.BYMONTH) - 1] ?? `month ${p.BYMONTH}`}` : ""}${days}${/EXDATE/i.test(rrule) ? " (with exclusions)" : ""}`;
    case "DAILY":
      return interval === 1 ? "daily" : `every ${interval} days`;
    default:
      // Never the raw RRULE (#541): it stays in the form's advanced input.
      return "custom schedule";
  }
}

export function today(): string {
  return new Date().toISOString().slice(0, 10);
}
