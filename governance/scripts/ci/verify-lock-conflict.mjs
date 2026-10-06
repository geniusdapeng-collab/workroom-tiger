// Legacy CLI delegates to the canonical root and preserves its real exit status.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const result = spawnSync(process.execPath, [fileURLToPath(new URL('../../../scripts/ci/verify-lock-conflict.mjs', import.meta.url)), ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error || result.signal || result.status === null) { console.error('Canonical protocol CLI could not complete'); process.exitCode = 1; }
else process.exitCode = result.status;
