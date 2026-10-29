import { useState } from "react";

/** Busy/error state for one wizard step. `run` clears the error, marks the
 * step busy while `fn` runs, and shows whatever `fn` throws as the error. */
export function useStepRunner() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return { busy, error, run };
}
