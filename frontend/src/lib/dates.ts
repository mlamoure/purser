/* Dates for display — one format each for the two kinds of time the API
 * sends (issue #540).
 *
 * - A calendar day ("2026-11-17": a ledger date, an as-of day, a vest date)
 *   names a day, not an instant. It is formatted from its own digits and
 *   never passes through a Date, so no time zone can move it to the day
 *   before ("2026-11-17" parsed by Date is UTC midnight, which is the 16th
 *   anywhere west of Greenwich).
 * - A timestamp ("2026-11-17T14:42:00Z": created, last used, fetched) is an
 *   instant, shown in the household time zone (GET /clock, the zone Settings
 *   sets and the ledger day follows) so every viewer reads the same wall
 *   time the ledger uses. useFormatTimestamp() in clock.ts supplies the zone.
 *
 * Formats: "Nov 17, 2026" and "Nov 17, 2026, 9:42 AM" (en-US, like money). */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const CALENDAR_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A calendar day for display: "2026-11-17" -> "Nov 17, 2026". null/""
 * shows "—"; anything that is not YYYY-MM-DD is returned as is. */
export function formatDate(day: string | null | undefined): string {
  if (day == null || day === "") return "—";
  const m = CALENDAR_DAY.exec(day);
  if (!m) return day;
  const month = MONTHS[Number(m[2]) - 1];
  if (!month) return day;
  return `${month} ${Number(m[3])}, ${m[1]}`;
}

const timestampFormats = new Map<string, Intl.DateTimeFormat>();

function timestampFormat(timeZone: string | undefined): Intl.DateTimeFormat {
  const key = timeZone ?? "";
  let format = timestampFormats.get(key);
  if (!format) {
    const options: Intl.DateTimeFormatOptions = {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    };
    try {
      format = new Intl.DateTimeFormat("en-US", { ...options, timeZone });
    } catch {
      // An unknown zone name: fall back to the browser's zone, not an error.
      format = new Intl.DateTimeFormat("en-US", options);
    }
    timestampFormats.set(key, format);
  }
  return format;
}

/** An instant for display in `timeZone` (IANA name; the browser's zone when
 * omitted): "Nov 17, 2026, 9:42 AM". null/"" shows "—"; an unparseable
 * string is returned as is. */
export function formatTimestamp(iso: string | null | undefined, timeZone?: string): string {
  if (iso == null || iso === "") return "—";
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return iso;
  // Newer ICU puts a narrow no-break space before AM/PM; a plain space keeps
  // the text stable to copy, search and test.
  return timestampFormat(timeZone).format(instant).replace(/[  ]/g, " ");
}

/** `days` days after (or before, when negative) a calendar day, as a
 * calendar day: UTC arithmetic, so no zone or DST change moves it. */
export function addDays(day: string, days: number): string {
  const m = CALENDAR_DAY.exec(day);
  if (!m) return day;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + days));
  return d.toISOString().slice(0, 10);
}

/** A field that holds a calendar day in some rows and an instant in others
 * (a schedule's "captured" stamp): each shown in its own format. */
export function formatDayOrTimestamp(value: string | null | undefined, timeZone?: string): string {
  if (value != null && CALENDAR_DAY.test(value)) return formatDate(value);
  return formatTimestamp(value, timeZone);
}
