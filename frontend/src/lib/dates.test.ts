import { afterEach, describe, expect, it, vi } from "vitest";
import { addDays, formatDate, formatDayOrTimestamp, formatTimestamp } from "./dates";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("formatDate (calendar days)", () => {
  it("formats a ledger day", () => {
    expect(formatDate("2026-11-17")).toBe("Nov 17, 2026");
    expect(formatDate("2027-01-01")).toBe("Jan 1, 2027");
  });

  it("never shifts the day in a zone west of UTC", () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    // What this guards against: Date reads "2026-11-17" as UTC midnight,
    // which is still the 16th in Los Angeles.
    expect(new Date("2026-11-17").getDate()).toBe(16);
    expect(formatDate("2026-11-17")).toBe("Nov 17, 2026");
    expect(formatDate("2026-01-01")).toBe("Jan 1, 2026");
  });

  it("shows a dash for no date and anything else as is", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("")).toBe("—");
    expect(formatDate("2026-13-01")).toBe("2026-13-01");
    expect(formatDate("not a date")).toBe("not a date");
  });
});

describe("formatTimestamp (instants)", () => {
  it("shows the instant in the household time zone", () => {
    expect(formatTimestamp("2026-11-17T14:42:00Z", "America/New_York")).toBe(
      "Nov 17, 2026, 9:42 AM",
    );
    expect(formatTimestamp("2026-11-17T14:42:00Z", "UTC")).toBe("Nov 17, 2026, 2:42 PM");
  });

  it("uses the household zone, not the browser's, across midnight", () => {
    vi.stubEnv("TZ", "Asia/Tokyo");
    // 03:30 UTC on the 18th is the evening of the 17th in Los Angeles.
    expect(formatTimestamp("2026-11-18T03:30:00Z", "America/Los_Angeles")).toBe(
      "Nov 17, 2026, 7:30 PM",
    );
  });

  it("falls back on bad input", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp("garbage", "UTC")).toBe("garbage");
    // An unknown zone name falls back to the browser's zone, not an error.
    expect(formatTimestamp("2026-11-17T14:42:00Z", "Not/AZone")).toMatch(/^Nov 1[78], 2026, /);
  });
});

describe("formatDayOrTimestamp and addDays", () => {
  it("formats each kind in its own format", () => {
    expect(formatDayOrTimestamp("2026-11-17", "UTC")).toBe("Nov 17, 2026");
    expect(formatDayOrTimestamp("2026-11-17T14:42:00+00:00", "UTC")).toBe("Nov 17, 2026, 2:42 PM");
  });

  it("adds calendar days across month ends and a DST change", () => {
    vi.stubEnv("TZ", "America/New_York");
    expect(addDays("2026-10-28", 10)).toBe("2026-11-07"); // DST ends Nov 1
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  });
});
