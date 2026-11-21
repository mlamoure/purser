/* A time of day as the sync schedule stores it (SyncSchedule.hour/minute):
 * a clock time in the household time zone (decision 111). */

export type TimeOfDay = { hour: number; minute: number };

/** "06:30" */
export function formatTimeOfDay(time: TimeOfDay): string {
  return `${String(time.hour).padStart(2, "0")}:${String(time.minute).padStart(2, "0")}`;
}

/** "06:30" -> { hour: 6, minute: 30 }; null for anything else. */
export function parseTimeOfDay(text: string): TimeOfDay | null {
  const m = /^(\d{2}):(\d{2})$/.exec(text);
  if (!m) return null;
  const hour = Number(m[1]);
  const minute = Number(m[2]);
  if (hour > 23 || minute > 59) return null;
  return { hour, minute };
}
