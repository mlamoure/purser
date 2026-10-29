import { queryOptions } from "@tanstack/react-query";
import { api } from "./api";

/** The signed-in session (`GET /auth/me`; null when there is none). App gates
 * every page on it, and any other reader of the session (useIsAdmin) uses
 * these same options: a second ["me"] observer with a different queryFn would
 * replace this one, and the next invalidation would refetch through it. */
export const meQuery = queryOptions({
  queryKey: ["me"],
  queryFn: async () => {
    const { data, response } = await api.GET("/auth/me");
    if (response.status === 401) return null;
    return data ?? null;
  },
});
