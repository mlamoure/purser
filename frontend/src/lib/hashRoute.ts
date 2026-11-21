/* The SPA routes on the URL hash (HashRouter: /#/selection?id=…), while links
 * from outside name a path: MCP `present` returns {public URL}/selection?id=…
 * and the server answers any path it does not own with this app. Without a
 * hash, HashRouter would open the Dashboard and drop the link's target, so
 * main.tsx moves such a path into the hash before the router starts (#542).
 * Paths the API owns (/accounts/…, /config, /tokens, …) never reach the SPA:
 * the server answers them itself. */

type UrlParts = { pathname: string; search: string; hash: string };

/** The hash-route URL for a path-style URL, or null when there is nothing to
 * move: the root, or a URL that already carries a hash route. */
export function hashRouteFor({ pathname, search, hash }: UrlParts): string | null {
  if (hash && hash !== "#") return null;
  if (pathname === "/" || pathname === "" || pathname === "/index.html") return null;
  return `/#${pathname}${search}`;
}
