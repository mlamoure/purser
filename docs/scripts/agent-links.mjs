// A .md.txt file has a different directory depth from its HTML page; the
// combined corpus has no page-relative context. Give both absolute doc links.
//
// Pages link page-relative (`../quickstart/` from tutorials/ai-setup,
// `../../how-to/x/` from a nested page), so each link resolves against the URL
// of the page it was written on (#462): a .md.txt file's page follows from its
// path, and in llms-full.txt / llms-small.txt each page starts with its title
// as a `# ` heading, which the .md.txt frontmatter maps back to the page.
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { join, relative } from 'node:path';
const root = new URL('../dist/', import.meta.url).pathname;
const site = process.env.DOCS_SITE || 'http://localhost:4321';
const base = (process.env.DOCS_BASE || '').replace(/^\/+|\/+$/g, '');
const canonical = new URL(base ? `/${base}/` : '/', site).href;
const relativeLink = /\]\(((?:\.\.\/|\.\/)+[^\s)]*)\)/g;

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  return (await Promise.all(entries.map(e => e.isDirectory() ? walk(join(dir, e.name)) : join(dir, e.name)))).flat();
}

function absolute(text, page) {
  return text.replace(relativeLink, (_, target) => `](${new URL(target, page).href})`);
}

// dist/tutorials/quickstart.md.txt -> <canonical>tutorials/quickstart/; the
// home page's file is dist/.md.txt -> <canonical>.
function pageUrl(file) {
  const rel = relative(root, file).slice(0, -'.md.txt'.length);
  return new URL(rel ? `${rel}/` : '', canonical).href;
}

function frontmatterTitle(text) {
  const front = text.match(/^---\n([\s\S]*?)\n---/);
  const line = front && front[1].match(/^title:\s*(.+)$/m);
  return line ? line[1].trim().replace(/^(['"])(.*)\1$/, '$2') : null;
}

const files = await walk(root);
const pageFiles = files.filter(f => f.endsWith('.md.txt'));
const corpora = files.filter(f => /^llms.*\.txt$/.test(relative(root, f)));

// Page title -> page URL; text before the first known title is the home page.
const pages = new Map();
for (const file of pageFiles) {
  const page = pageUrl(file);
  const title = frontmatterTitle(await readFile(file, 'utf8'));
  if (!title) throw new Error(`${relative(root, file)}: no title in frontmatter`);
  if (pages.has(title)) throw new Error(`two pages titled "${title}": agent links cannot be resolved per page`);
  pages.set(title, page);
}

for (const file of pageFiles) {
  const page = pageUrl(file);
  await writeFile(file, absolute(await readFile(file, 'utf8'), page));
}

for (const file of corpora) {
  let page = canonical;
  let fenced = false;
  const lines = (await readFile(file, 'utf8')).split('\n').map(line => {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    const heading = !fenced && line.match(/^# (.+)$/);
    if (heading && pages.has(heading[1].trim())) page = pages.get(heading[1].trim());
    return absolute(line, page);
  });
  await writeFile(file, lines.join('\n'));
}
