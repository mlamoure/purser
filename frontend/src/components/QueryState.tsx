import type { ReactNode } from "react";
import { ErrorText } from "./ui";

/** The parts of a TanStack Query result QueryState reads. */
export type QueryLike<T> = {
  data: T | undefined;
  error: Error | null;
  isLoading: boolean;
};

/** One place for the three states a loaded list or panel can be in besides
 * "has data" (issue #540): loading, failed, and empty.
 *
 * A failed load shows "Could not load <what>: <server detail>" — never an
 * empty list or a spinner that never ends. The queryFn must throw for that:
 * `if (error) throw new Error(errorDetail(error))`; a queryFn that returns
 * `data ?? []` on an error turns every failure into "nothing here".
 *
 * Data refetched after a failure keeps showing under the error. A query that
 * is disabled (not loading, no data) renders nothing. Without `children` it
 * is a status line only: the caller renders the data itself. */
export function QueryState<T>({
  query,
  what,
  empty,
  isEmpty = (data) => Array.isArray(data) && data.length === 0,
  children,
}: {
  query: QueryLike<T>;
  /** What failed to load, in words: "accounts", "users". */
  what: string;
  /** Shown instead of `children` when the data is empty (see `isEmpty`); a
   * string becomes a muted paragraph. */
  empty?: ReactNode;
  isEmpty?: (data: T) => boolean;
  children?: (data: T) => ReactNode;
}) {
  const failed = query.error ? (
    <ErrorText>{`Could not load ${what}: ${query.error.message}`}</ErrorText>
  ) : null;
  if (query.data === undefined) {
    if (failed) return failed;
    if (query.isLoading)
      return (
        <p role="status" className="text-sm text-muted-foreground">
          Loading {what}…
        </p>
      );
    return null;
  }
  const body =
    empty !== undefined && isEmpty(query.data) ? (
      typeof empty === "string" ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        empty
      )
    ) : (
      children?.(query.data)
    );
  return (
    <>
      {failed}
      {body}
    </>
  );
}
