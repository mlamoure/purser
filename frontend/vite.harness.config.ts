/* Dev server for the widget harness (mock MCP Apps host). Serves the whole
 * frontend dir so the harness can fetch built bundles from /dist-widgets.
 * Dev-only — never built or shipped. */

import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  server: {
    open: "/widgets/harness/index.html",
    port: 5177,
  },
});
