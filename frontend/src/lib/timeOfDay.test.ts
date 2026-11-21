import { describe, expect, it } from "vitest";
import { formatTimeOfDay, parseTimeOfDay } from "./timeOfDay";

describe("parseTimeOfDay / formatTimeOfDay", () => {
  it("reads and writes HH:MM", () => {
    expect(parseTimeOfDay("07:05")).toEqual({ hour: 7, minute: 5 });
    expect(formatTimeOfDay({ hour: 7, minute: 5 })).toBe("07:05");
    expect(parseTimeOfDay("24:00")).toBeNull();
    expect(parseTimeOfDay("7:05")).toBeNull();
    expect(parseTimeOfDay("")).toBeNull();
  });
});
