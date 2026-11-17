import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

// Exercise the actual delivery function without GitHub or a source mutation.
const root = process.cwd();
const fixture = mkdtempSync(join(tmpdir(), "aiops-delivery-"));
try {
  writeFileSync(join(fixture, "git"), '#!/bin/sh\ncase "$1" in branch|rev-parse) echo task;; esac\n', {mode: 0o700});
  writeFileSync(join(fixture, "gh"), `#!/usr/bin/env python3
import os, sys
from pathlib import Path
with Path(os.environ['CALL_LOG']).open('a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['pr', 'create']:
    if os.environ['FAIL_AT'] == 'pr': raise SystemExit(1)
    print('https://example.test/pr/1')
if sys.argv[1:3] == ['issue', 'edit'] and os.environ['FAIL_AT'] == 'label': raise SystemExit(1)
`, {mode: 0o700});
  mkdirSync(join(fixture, ".sandcastle"));
  writeFileSync(join(fixture, ".sandcastle/policy-check.mjs"), "");
  writeFileSync(join(fixture, "driver.mts"),
    `import {openDeliveryPr,selectReadyIssues,extractClaimedIssues} from ${JSON.stringify(resolve(".sandcastle/planner.ts"))};
import assert from 'node:assert/strict';
openDeliveryPr('task', [{number:42,title:'candidate'}]);
const issue={number:42,title:'candidate',branch:'task'};
assert.deepEqual(selectReadyIssues([issue],new Set(),extractClaimedIssues([])),[]);
assert.equal(selectReadyIssues([issue],new Set([42]),extractClaimedIssues([])).length,1);
`);
  for (const failAt of ["pr", "label", "none"]) {
    const log = join(fixture, "calls");
    writeFileSync(log, "");
    const result = spawnSync(process.execPath,
      ["--import", join(root, "node_modules/tsx/dist/loader.mjs"), join(fixture, "driver.mts")],
      {cwd: fixture, encoding: "utf8", env: {...process.env, PATH: `${fixture}:${process.env.PATH}`,
        CALL_LOG: log, FAIL_AT: failAt}});
    assert.equal(result.status === 0, failAt === "none", result.stderr);
    const calls = readFileSync(log, "utf8");
    assert.equal(calls.includes("issue edit 42 --remove-label ready-for-agent"), failAt !== "pr");
    if (failAt !== "pr") assert.ok(calls.indexOf("pr create") < calls.indexOf("issue edit"));
  }
} finally {
  rmSync(fixture, {recursive: true, force: true});
}
console.log("staged delivery lifecycle passed: creation failure, label failure, success and reauthorization");
