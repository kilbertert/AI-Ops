import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
const child = spawnSync(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', `
  const { cancellationSignal } = await import('./.sandcastle/queue/cancellation.ts');
  cancellationSignal.addEventListener('abort', () => { console.log('abort received'); process.exit(0); });
  process.kill(process.pid, 'SIGINT');
  setTimeout(() => process.exit(1), 1000);
`], { encoding: 'utf8' });
assert.equal(child.status, 0, child.stderr);
assert.match(child.stdout, /abort received/);
console.log('runner cancellation signal check passed');
