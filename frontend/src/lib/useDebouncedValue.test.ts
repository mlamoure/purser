import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDebouncedValue } from "./useDebouncedValue";

describe("useDebouncedValue (#141)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("holds the first value until the input has been still for the delay", () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: "" },
    });
    for (const value of ["s", "st", "sta", "star"]) {
      rerender({ value });
      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(result.current).toBe("");
    }
    // 100 ms have passed since the last keystroke; the value lands at 250.
    act(() => {
      vi.advanceTimersByTime(149);
    });
    expect(result.current).toBe("");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe("star");
  });

  it("a value that stops changing settles once and stays", () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: 1 },
    });
    rerender({ value: 2 });
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(result.current).toBe(2);
    rerender({ value: 2 });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(2);
  });
});
