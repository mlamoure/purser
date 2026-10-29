import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The SPA consumes the same public REST API agents use; in dev, proxy to a
// locally running `purser serve`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // The licences of every package bundled into the SPA (#429). Minification
    // drops their comments, so the build writes them beside the bundle; the
    // footer links it, and the server serves it at /third-party-licenses.txt.
    license: { fileName: "third-party-licenses.txt" },
  },
  server: {
    proxy: Object.fromEntries(
      [
        "/auth", "/accounts", "/categories", "/transactions", "/connections",
        "/matching", "/schedules", "/forecast", "/networth", "/holdings",
        "/events", "/webhooks", "/import", "/categorize", "/tokens", "/users",
        "/audit", "/config", "/healthz", "/source",
      ].map((p) => [p, "http://localhost:8000"])
    ),
  },
});
