import assert from 'node:assert/strict';
import { mkdtemp, mkdir, copyFile, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

for (const home of ['.md.txt', 'index.md.txt']) {
  for (const base of ['', '/purser']) {
    test(`homepage ${home}, base ${base || '/'}`, async () => {
      const root = await mkdtemp(join(tmpdir(), 'purser-agent-links-'));
      try {
        await mkdir(join(root, 'scripts'));
        await mkdir(join(root, 'dist', 'tutorials'), { recursive: true });
        await copyFile(new URL('../docs/scripts/agent-links.mjs', import.meta.url), join(root, 'scripts', 'agent-links.mjs'));
        await writeFile(join(root, 'dist', home), '---\ntitle: Home\n---\n[Start](./tutorials/quickstart/)\n');
        await writeFile(join(root, 'dist', 'tutorials', 'quickstart.md.txt'), '---\ntitle: Quickstart\n---\n[Home](../../)\n');
        await writeFile(join(root, 'dist', 'llms-full.txt'), '# Home\n[Start](./tutorials/quickstart/)\n# Quickstart\n[Home](../../)\n');
        execFileSync(process.execPath, [join(root, 'scripts', 'agent-links.mjs')], {
          env: { ...process.env, DOCS_SITE: 'https://docs.example.com', DOCS_BASE: base },
        });
        const canonical = `https://docs.example.com${base}/`;
        assert.match(await readFile(join(root, 'dist', home), 'utf8'), new RegExp(`\\]\\(${canonical}tutorials/quickstart/\\)`));
        const corpus = await readFile(join(root, 'dist', 'llms-full.txt'), 'utf8');
        assert.ok(corpus.includes(`[Start](${canonical}tutorials/quickstart/)`));
        assert.ok(corpus.includes(`[Home](${canonical})`));
        assert.ok(!corpus.includes('/index/'));
      } finally {
        await rm(root, { recursive: true, force: true });
      }
    });
  }
}
