import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "../api/schema";

// The SPA consumes the SAME public REST API agents use (session cookie auth).
export const api = createClient<paths>({
  baseUrl: "/",
  credentials: "include",
});

// Mid-session expiry (#220): when any request 401s, notify the app shell so
// it can invalidate the "me" query and fall back to the login gate — without
// this, an expired session renders as a generic error on whatever page the
// user happens to be on. /auth/* is excluded: the initial /auth/me probe and
// a wrong password on /auth/login 401 by design.
let onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

const unauthorizedMiddleware: Middleware = {
  onResponse({ request, response }) {
    if (response.status === 401 && !new URL(request.url).pathname.startsWith("/auth/")) {
      onUnauthorized?.();
    }
    return undefined;
  },
};

api.use(unauthorizedMiddleware);

export function errorDetail(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    return JSON.stringify(detail);
  }
  return "request failed";
}
