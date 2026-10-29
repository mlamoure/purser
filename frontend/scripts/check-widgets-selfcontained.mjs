/* Guardrail 8 enforcement: built widget bundles must be fully
 * self-contained — no external scripts, styles, fonts, images, or fetches.
 * Scans dist-widgets/*.html for anything that would leave the iframe.
 * (xmlns="http://..." attributes are namespace identifiers, not fetches,
 * and are not matched by these patterns.) */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const PATTERNS = [
  [/(?:src|href)\s*=\s*["']\s*(?:https?:)?\/\//gi, "external src/href"],
  // `(?<!new\s+)`: JS's `new URL(...)` constructor is not CSS url() — it is
  // checked by its own pattern below.
  [/(?<!\bnew\s+)url\(\s*["']?\s*(?:https?:)?\/\//gi, "external url() in CSS"],
  // A JS URL constructor with an absolute http(s) URL. One known-benign case is
  // allowed: @cfworker/json-schema (pulled in by the MCP v2 client, ext-apps 2)
  // uses `new URL("https://github.com/cfworker")` as the fallback BASE for
  // resolving $refs when there is no window location — a constant, never fetched.
  [/new\s+URL\(\s*["'](?:https?:)?\/\/(?!github\.com\/cfworker["'])/g, "external new URL()"],
  [/@import\s+["'](?:https?:)?\/\//gi, "external @import"],
  [/fetch\(\s*["'`](?:https?:)?\/\//gi, "external fetch()"],
  [/new\s+WebSocket\(/gi, "WebSocket"],
  [/XMLHttpRequest/g, "XMLHttpRequest"],
];

let files;
try {
  files = readdirSync("dist-widgets").filter((f) => f.endsWith(".html"));
} catch {
  console.error("dist-widgets/ missing — run `npm run build:widgets` first");
  process.exit(1);
}
if (files.length === 0) {
  console.error("dist-widgets/ contains no bundles");
  process.exit(1);
}

let failed = false;
for (const file of files) {
  const html = readFileSync(join("dist-widgets", file), "utf8");
  for (const [pattern, label] of PATTERNS) {
    pattern.lastIndex = 0;
    const match = pattern.exec(html);
    if (match) {
      const at = html.slice(Math.max(0, match.index - 40), match.index + 60).replace(/\s+/g, " ");
      console.error(`✗ ${file}: ${label} — …${at}…`);
      failed = true;
    }
  }
  if (!failed) console.log(`✓ ${file} is self-contained`);
}

process.exit(failed ? 1 : 0);
