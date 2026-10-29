/* The mocked API client every component test runs against (issue #209).
 *
 * The SPA reaches Purser only through the openapi-fetch client exported as
 * `api` from src/lib/api.ts. setup.ts swaps that export for `apiMock` below in
 * every test file, so no test can reach the network, and a request the test
 * did not route fails the test (see setup.ts) instead of passing silently.
 * The swap happens at the client, not at `fetch`: openapi-fetch builds
 * `new Request("/path")`, which Node's Request cannot resolve without a base
 * URL, and asserting on the client call keeps tests in the same terms as the
 * component code (`api.POST("/users", { body })`).
 *
 * Usage:
 *   mockApi({
 *     "GET /users": ok([]),
 *     "POST /users": fail(409, "user already exists"),
 *     "GET /connections/{connection_id}/discovered": (init) => ok(rowsFor(init)),
 *   });
 *   expect(apiMock.POST).toHaveBeenCalledWith("/users", { body: { ... } });
 *
 * Route keys are the HTTP method plus the OpenAPI path template exactly as the
 * component passes it; path/query params and the body arrive in `init`. */

import { vi } from "vitest";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/** The second argument of an openapi-fetch call. */
export type CallInit = {
  params?: { path?: Record<string, unknown>; query?: Record<string, unknown> };
  body?: unknown;
};

/** One mocked response in openapi-fetch terms: `data` on success, `error`
 * (the parsed error body) otherwise. `status` defaults to 200, or 400 when
 * `error` is set. */
export type Reply = { data?: unknown; error?: unknown; status?: number };

/** A fixed reply, or a function of the call's `init` for replies that depend
 * on the request or change between calls. */
export type Route = Reply | ((init: CallInit) => Reply | Promise<Reply>);

/** Success reply carrying `data`. */
export const ok = (data?: unknown, status = 200): Reply => ({ data, status });

/** Error reply shaped like a FastAPI error body (`{"detail": ...}`). */
export const fail = (status: number, detail: unknown): Reply => ({ error: { detail }, status });

let routes = new Map<string, Route>();
let unrouted: string[] = [];

function handler(method: Method) {
  return async (path: string, init?: CallInit) => {
    const key = `${method} ${path}`;
    const route = routes.get(key);
    if (route === undefined) {
      unrouted.push(key);
      throw new Error(`unmocked API call: ${key}`);
    }
    const reply = typeof route === "function" ? await route(init ?? {}) : route;
    const status = reply.status ?? (reply.error === undefined ? 200 : 400);
    return { data: reply.data, error: reply.error, response: new Response(null, { status }) };
  };
}

/** Stands in for `api`; assert on its methods' calls. */
export const apiMock = {
  GET: vi.fn(handler("GET")),
  POST: vi.fn(handler("POST")),
  PUT: vi.fn(handler("PUT")),
  PATCH: vi.fn(handler("PATCH")),
  DELETE: vi.fn(handler("DELETE")),
};

/** Route calls for the current test; a later call adds to (or replaces keys
 * of) the earlier ones. Everything is cleared after each test. */
export function mockApi(table: Record<string, Route>): void {
  for (const [key, route] of Object.entries(table)) routes.set(key, route);
}

/** Clear the routes and return the calls nobody routed (setup.ts, afterEach). */
export function resetApiMock(): string[] {
  const missed = unrouted;
  routes = new Map();
  unrouted = [];
  return missed;
}
