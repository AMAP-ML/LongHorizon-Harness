import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const directory = await mkdtemp(join(tmpdir(), 'lh-harness-core-test-'));
const outfile = join(directory, 'runFeed.test.mjs');
const localRequire = createRequire(import.meta.url);
let build;
try {
  ({ build } = localRequire('esbuild'));
} catch {
  const frontendRequire = createRequire(new URL('../../web/package.json', import.meta.url));
  ({ build } = frontendRequire('esbuild'));
}

try {
  await build({
    entryPoints: [resolve('test/runFeed.test.ts')],
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    outfile,
  });
  await new Promise((resolveRun, rejectRun) => {
    const child = spawn(process.execPath, ['--test', outfile], { stdio: 'inherit' });
    child.on('error', rejectRun);
    child.on('exit', (code) => code === 0 ? resolveRun() : rejectRun(new Error(`node --test exited with ${code}`)));
  });
} finally {
  await rm(directory, { recursive: true, force: true });
}
