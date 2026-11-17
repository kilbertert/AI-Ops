"""任务引用不要求提前关闭验收，也不能让已占用任务被重复派发。"""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_linked_issue_forms_keep_order_and_do_not_claim_prose():
    script = """
import assert from 'node:assert/strict';
import { linkedIssues, primaryIssue } from './.sandcastle/linked-issues.mjs';
assert.deepEqual(linkedIssues('Refs #42\\nCloses #7\\nFixes #42\\nResolves #9'), [42, 7, 9]);
assert.deepEqual(linkedIssues('refs #42\\nSee #12\\nprefixes #8\\nRefs #42abc'), [42]);
assert.deepEqual(linkedIssues(), []);
assert.equal(primaryIssue('Refs #42\\nCloses #43'), 43);
assert.equal(primaryIssue('Refs #42'), 42);
assert.equal(primaryIssue(), undefined);
"""
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_workflow_refuses_duplicate_pr_for_unclosed_acceptance(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/agent-implement.yml").read_text())
    step = next(
        s for j in workflow["jobs"].values() for s in j.get("steps", []) if s.get("id") == "preflight"
    )
    script = step["run"].replace("${{ steps.ticket.outputs.implementable }}", "true")
    fake = tmp_path / "gh"
    fake.write_text("""#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
expression = sys.argv[sys.argv.index('--jq') + 1]
subprocess.run(['jq', expression], input=Path(os.environ['PR_FIXTURE']).read_bytes(), check=True)
""")
    fake.chmod(0o700)
    fixture, output = tmp_path / "prs.json", tmp_path / "output"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ISSUE_NUMBER": "42",
        "PR_FIXTURE": str(fixture),
        "GITHUB_OUTPUT": str(output),
    }
    for body, refused in [("Refs #42", True), ("Closes #42", True), ("Refs #420", False)]:
        fixture.write_text(json.dumps([{"number": 1, "url": "https://example.test/pr/1", "body": body}]))
        output.write_text("")
        subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True)
        assert f"refused={str(refused).lower()}" in output.read_text()
