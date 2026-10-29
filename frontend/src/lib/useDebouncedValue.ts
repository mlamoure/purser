import { useEffect, useState } from "react";

/** `value`, but only once it has stopped changing for `delayMs` (#141). The
 * register search box feeds its query key from this, so typing a word sends
 * one search instead of one per keystroke — each of which decrypts the
 * account's descriptions server-side. */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
