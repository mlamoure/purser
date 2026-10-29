// Check the emitted files, including relative links that the Starlight validator skips.
import { readFile, readdir, access } from 'node:fs/promises';
import { join, relative } from 'node:path';
const root = new URL('../dist/', import.meta.url).pathname;
const site = process.env.DOCS_SITE || 'http://localhost:4321';
const base = '/' + (process.env.DOCS_BASE || '').replace(/^\/+|\/+$/g, '');
const prefix = base === '/' ? '/' : base + '/';
const failures = [];
async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  return (await Promise.all(entries.map(e => e.isDirectory() ? walk(join(dir, e.name)) : join(dir, e.name)))).flat();
}
const files = await walk(root);
const ids = new Map();
for (const file of files.filter(f => f.endsWith('.html'))) {
  const text = await readFile(file, 'utf8');
  ids.set(file, new Set([...text.matchAll(/\bid="([^"]+)"/g)].map(m => m[1])));
}
for (const file of ids.keys()) {
  const current = new URL(prefix + relative(root, file).replace(/index\.html$/, ''), site);
  const text = await readFile(file, 'utf8');
  for (const match of text.matchAll(/\b(?:href|src)="([^"]+)"/g)) {
    const value = match[1].replaceAll('&amp;', '&');
    const target = new URL(value, current);
    if (target.origin !== new URL(site).origin) continue;
    // Astro's 404 canonical URL is metadata, not a navigable generated page.
    if (file.endsWith('/404.html') && target.pathname === prefix + '404/') continue;
    if (!target.pathname.startsWith(prefix)) { failures.push(`${current.pathname}: outside base: ${value}`); continue; }
    let path = join(root, decodeURIComponent(target.pathname.slice(prefix.length)));
    if (target.pathname.endsWith('/')) path = join(path, 'index.html');
    try { await access(path); } catch { failures.push(`${current.pathname}: missing: ${value}`); continue; }
    if (target.hash && ids.has(path) && !ids.get(path).has(decodeURIComponent(target.hash.slice(1)))) failures.push(`${current.pathname}: missing anchor: ${value}`);
  }
}
for (const name of ['llms.txt', 'llms-full.txt', 'llms-small.txt', 'openapi.json', 'pagefind/pagefind.js', 'favicon.svg']) await access(join(root, name));
if (!(await readFile(join(root, 'llms.txt'), 'utf8')).includes(new URL(prefix, site).href)) failures.push('llms.txt lacks canonical base URL');
if (!files.some(f => f.endsWith('.md.txt'))) failures.push('no per-page Markdown');
for (const file of files.filter(f => f.endsWith('.md.txt') || /llms.*\.txt$/.test(f))) {
  const text = await readFile(file, 'utf8');
  if (/\]\((?:\.\.?\/)/.test(text)) failures.push(`${relative(root, file)}: relative agent link`);
}
if (failures.length) throw new Error(failures.slice(0,30).join('\n') + `\n${failures.length} broken links`);
console.log(`Verified ${ids.size} HTML pages, local assets, anchors, search and agent surfaces at ${new URL(prefix,site)}`);
