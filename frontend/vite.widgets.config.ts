/* Widget bundle build (issue #72): one fully self-contained HTML file per
 * widget. vite-plugin-singlefile inlines every script/style into the page,
 * which is what MCP Apps hosts require (sandboxed iframes, restrictive
 * CSPs — guardrail 8). Single-entry by design; scripts/build-widgets.mjs
 * loops the widgets and sets WIDGET=<name>. */

import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const widget = process.env.WIDGET;
if (!widget) {
  throw new Error("WIDGET env var required (use `npm run build:widgets`)");
}

export default defineConfig({
  root: `widgets/${widget}`,
  plugins: [viteSingleFile()],
  build: {
    outDir: "../../dist-widgets",
    emptyOutDir: false,
    chunkSizeWarningLimit: 900,
    // Bundled packages' licences (#429), as JSON: build-widgets.mjs folds them
    // into a trailing HTML comment so each self-contained bundle carries its
    // own notices, then deletes the file.
    license: { fileName: `${widget}.licenses.json` },
  },
});
