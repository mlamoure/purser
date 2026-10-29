// A .md.txt file has a different directory depth from its HTML page; the
// combined corpus has no page-relative context. Give both absolute doc links.
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
const root = new URL('../dist/', import.meta.url).pathname;
const site = process.env.DOCS_SITE || 'http://localhost:4321';
const base = (process.env.DOCS_BASE || '').replace(/^\/+|\/+$/g, '');
const canonical = new URL(base ? `/${base}/` : '/', site).href;
async function normalize(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) { await normalize(path); continue; }
    if (!entry.name.endsWith('.md.txt') && !/^llms.*\.txt$/.test(entry.name)) continue;
    const text = await readFile(path, 'utf8');
    await writeFile(path, text.replace(/\]\((?:\.\.\/|\.\/)+([^\s)]+)\)/g, (_, target) => `](${new URL(target, canonical).href})`));
  }
}
await normalize(root);
