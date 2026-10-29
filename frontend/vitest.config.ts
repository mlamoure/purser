/* Component/unit tests for the SPA (issue #209): vitest + Testing Library in
 * jsdom, headless (`npm test` = `vitest run`). Extends vite.config.ts so tests
 * compile through the same plugins as the build. Test files sit next to the
 * code they cover (`Foo.test.tsx` beside `Foo.tsx`) and are type-checked by
 * `tsc -b` along with it; the helpers they share live in src/test/. MCP
 * widget views are covered too (`widgets/<name>/view.test.ts`, #347), and
 * type-checked by `tsc -p tsconfig.widgets.json` with the widget tree. */

import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config.ts";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{ts,tsx}", "widgets/**/*.test.ts"],
      setupFiles: ["./src/test/setup.ts"],
    },
  }),
);
