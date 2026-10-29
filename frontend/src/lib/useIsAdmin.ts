import { useQuery } from "@tanstack/react-query";
import { meQuery } from "./me";

/** True when the signed-in session holds the admin scope. Reads the `me`
 * query App loads before any page renders, with App's own options (so an
 * invalidation still refetches /auth/me) and without fetching it again, so a
 * page can show admin-only controls (the server still enforces the scope). */
export function useIsAdmin(): boolean {
  const me = useQuery({ ...meQuery, enabled: false });
  const scopes = (me.data as { scopes?: string[] } | null | undefined)?.scopes ?? [];
  return scopes.includes("admin");
}
