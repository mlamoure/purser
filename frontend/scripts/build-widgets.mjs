/* Build every widget under widgets/<name>/index.html into a self-contained
 * dist-widgets/<name>.html (vite-plugin-singlefile is single-entry, so we
 * loop). Typechecks the widgets tree first. */

import { execSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
} from "node:fs";
import { join } from "node:path";

const NON_WIDGET_DIRS = new Set(["lib", "harness", "fixtures"]);

const widgets = readdirSync("widgets", { withFileTypes: true })
  .filter(
    (entry) =>
      entry.isDirectory() &&
      !NON_WIDGET_DIRS.has(entry.name) &&
      existsSync(join("widgets", entry.name, "index.html")),
  )
  .map((entry) => entry.name)
  .sort();

if (widgets.length === 0) {
  console.error("No widget entries found under widgets/*/index.html");
  process.exit(1);
}

execSync("npx tsc -p tsconfig.widgets.json", { stdio: "inherit" });

rmSync("dist-widgets", { recursive: true, force: true });
mkdirSync("dist-widgets", { recursive: true });

for (const widget of widgets) {
  console.log(`\n▸ building widget: ${widget}`);
  execSync("npx vite build --config vite.widgets.config.ts", {
    stdio: "inherit",
    env: { ...process.env, WIDGET: widget },
  });
  renameSync("dist-widgets/index.html", `dist-widgets/${widget}.html`);
  appendLicenses(widget);
}

/* Minification drops the bundled packages' licence comments, and a widget is
 * one self-contained file, so its notices travel inside it (#429): a trailing
 * HTML comment with each bundled package's licence text, identical texts once.
 * `--` cannot appear inside an HTML comment, so it is spaced out. */
function appendLicenses(widget) {
  const jsonPath = `dist-widgets/${widget}.licenses.json`;
  const packages = JSON.parse(readFileSync(jsonPath, "utf8"));
  rmSync(jsonPath);
  const byText = new Map();
  for (const pkg of packages) {
    const text = (pkg.text ?? `${pkg.identifier ?? "unknown"} (no licence file published)`).trim();
    const names = byText.get(text) ?? [];
    names.push(`${pkg.name} ${pkg.version} (${pkg.identifier ?? "unknown"})`);
    byText.set(text, names);
  }
  const sections = [...byText].map(([text, names]) => `${names.join("\n")}\n\n${text}`);
  const body = [
    `Third-party software bundled in this widget (${packages.length} packages).`,
    "Purser itself is AGPL-3.0-only; its server's GET /source links the source code.",
    ...sections,
  ]
    .join("\n\n==========\n\n")
    .replace(/--/g, "- -");
  appendFileSync(`dist-widgets/${widget}.html`, `\n<!--\n${body}\n-->\n`);
}

console.log(`\nBuilt ${widgets.length} widget bundle(s): ${widgets.join(", ")}`);
