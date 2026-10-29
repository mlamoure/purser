// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';
import starlightLlmsTxt from 'starlight-llms-txt';
import starlightMdTxt from 'starlight-md-txt';
import starlightOpenAPI, { openAPISidebarGroups } from 'starlight-openapi';

// One build for every target (issue #156): the deploy step sets DOCS_SITE (and
// DOCS_BASE for subpath hosting like GitHub Pages); the build itself never
// changes. site must be absolute — starlight-llms-txt derives llms.txt URLs
// from it.
const site = process.env.DOCS_SITE || 'http://localhost:4321';
const base = process.env.DOCS_BASE || undefined;

export default defineConfig({
  site,
  base,
  build: { format: 'directory' },
  integrations: [
    starlight({
      title: 'Purser',
      description: 'The finance backend for the personal agent stack.',
      favicon: '/favicon.svg',
      social: [
        { icon: 'github', label: 'Source on GitHub', href: 'https://github.com/mlamoure/purser' },
      ],
      plugins: [
        starlightOpenAPI([
          {
            base: 'reference/rest-api',
            // Synced from ../frontend/openapi.json by scripts/sync-openapi.mjs
            // (npm pre{dev,build} hook) so the same checked-in artifact feeds
            // the rendered reference AND ships at /openapi.json.
            schema: './public/openapi.json',
            sidebar: { label: 'REST API', collapsed: true },
          },
        ]),
        starlightLlmsTxt({
          projectName: 'Purser',
          description:
            'AI-native, self-hosted personal finance backend: double-entry ledger, SimpleFIN feeds, deterministic matching and forecasting, MCP + REST as the primary interfaces.',
        }),
        starlightMdTxt({ format: '.md.txt' }),
        starlightLinksValidator({ errorOnRelativeLinks: false }),
      ],
      sidebar: [
        { label: 'Tutorials', items: [{ autogenerate: { directory: 'tutorials' } }] },
        { label: 'How-to', items: [{ autogenerate: { directory: 'how-to' } }] },
        {
          label: 'Reference',
          items: [
            'reference/mcp-tools',
            'reference/configuration',
            'reference/cli',
            'reference/events-webhooks',
            ...openAPISidebarGroups,
          ],
        },
        { label: 'Explanation', items: [{ autogenerate: { directory: 'explanation' } }] },
      ],
    }),
  ],
});
