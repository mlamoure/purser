// The checked-in specification is the source of truth for rendering and download.
// The application and documentation share frontend/openapi.json in one repository.
import { copyFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const docsRoot = dirname(dirname(fileURLToPath(import.meta.url)));
copyFileSync(join(docsRoot, '..', 'frontend', 'openapi.json'), join(docsRoot, 'public', 'openapi.json'));
