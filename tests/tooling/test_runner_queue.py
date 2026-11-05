"""队列治理的独立判据：只清理关闭PR、标签隔离、过期交付拒绝。"""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_lifecycle_decisions():
    script = r"""
import assert from 'node:assert/strict';
import { current } from './.sandcastle/queue/pr-current.mjs';
import { prNumber, obsolete, completePages } from './.sandcastle/queue/retire-runs.mjs';
const repo = 'owner/example', head = 'a'.repeat(40);
const pr = {number: 7, state: 'closed', merged: true, closed_at: '2026-11-05T12:00:00Z',
  user: {login: 'owner'}, head: {ref: 'task', sha: head, repo: {full_name: repo}},
  base: {repo: {full_name: repo}}};
const run = {id: 1, path: '.github/workflows/agent-update-branch.yml',
  event: 'pull_request_target', display_title: 'Agent Update Branch / pr-7',
  status: 'queued', created_at: '2026-11-05T11:00:00Z', head_branch: 'task', repository: {full_name: repo}};
assert.equal(prNumber(run), '7');
assert.equal(obsolete(run, pr, repo), true);
for (const update of [{state: 'open'}, {number: 8}, {closed_at: null},
  {user: {login: 'stranger'}}, {head: {...pr.head, repo: {full_name: 'fork/example'}}}]) {
  assert.equal(obsolete(run, {...pr, ...update}, repo), false);
}
for (const update of [{path: '.github/workflows/cd.yml'}, {event: 'push'},
  {display_title: 'Old unstructured title'}, {status: 'completed'},
  {created_at: '2026-11-05T12:01:00Z'}, {repository: {full_name: 'another/repo'}}]) {
  assert.equal(obsolete({...run, ...update}, pr, repo), false);
}
assert.equal(obsolete(run, pr, repo, '2026-11-05T10:00:00Z'), false);
assert.equal(current({...pr, state: 'open', merged: false}, repo, '7', head), true);
assert.equal(current(pr, repo, '7', head), false);
assert.equal(current({...pr, state: 'open', merged: false}, repo, '7', 'b'.repeat(40)), false);
assert.throws(() => current({}, repo, '7', head));
assert.throws(() => completePages([{total_count: 101, workflow_runs: [run]}]));
assert.deepEqual(completePages([{total_count: 1, workflow_runs: [run]}]), [run]);
assert.throws(() => completePages([
  {total_count: 2, workflow_runs: [run]}, {total_count: 2, workflow_runs: [run]}]));
"""
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_closed_pr_gate_blocks_real_push_entry(tmp_path):
    fake = tmp_path / "gh"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"number":7,"state":"closed"}\'\n')
    fake.chmod(0o700)
    log = tmp_path / "git-called"
    git = tmp_path / "git"
    git.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    git.chmod(0o700)
    result = subprocess.run(
        ["bash", ".sandcastle/trusted-pr-delivery.sh", "push", ".", "a" * 40, "task"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GH_REPO": "owner/example",
            "PR_NUMBER": "7",
            "BRANCH_HEAD_SHA": "a" * 40,
            "AFK_DEFAULT_BRANCH": "main",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "PR is closed or superseded" in result.stdout
    assert "push " not in log.read_text()


def test_run_retirement_calls_only_matching_closed_pr(tmp_path):
    repo = "owner/example"
    run = {
        "id": 1,
        "path": ".github/workflows/agent-review.yml",
        "event": "pull_request_target",
        "display_title": "Agent Review / pr-7",
        "status": "in_progress",
        "created_at": "2026-11-05T11:00:00Z",
        "head_branch": "task",
        "repository": {"full_name": repo},
    }
    pr = {
        "number": 7,
        "state": "closed",
        "closed_at": "2026-11-05T12:00:00Z",
        "user": {"login": "owner"},
        "head": {"ref": "task", "repo": {"full_name": repo}},
        "base": {"repo": {"full_name": repo}},
    }
    (tmp_path / "fixture.json").write_text(json.dumps({"run": run, "pr": pr}))
    cli = tmp_path / "gh"
    cli.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ['FIXTURE']); data=json.loads((root/'fixture.json').read_text()); endpoint=sys.argv[2]
if 'status=' in endpoint:
    print(json.dumps([{'total_count':1,'workflow_runs':[data['run']]}]))
elif endpoint.endswith('/cancel'):
    (root/'cancelled').write_text(endpoint)
elif '/pulls/' in endpoint:
    print(json.dumps(data['pr']))
else:
    run=data['run']
    if (root/'cancelled').exists(): run['status']='completed'
    print(json.dumps(run))
""")
    cli.chmod(0o700)
    result = subprocess.run(
        ["node", ".sandcastle/queue/retire-runs.mjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FIXTURE": str(tmp_path),
            "GH_REPO": repo,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cancelled").read_text() == "repos/owner/example/actions/runs/1/cancel"
    assert result.stdout.count("Cancel 1:") == 1
    assert "terminal state confirmed" in result.stdout


def test_run_retirement_retries_drifting_listing(tmp_path):
    """run 列表最终一致：total_count 瞬时领先 items 时重试而不是拒绝清理。"""
    repo = "owner/example"
    run = {
        "id": 1,
        "path": ".github/workflows/agent-review.yml",
        "event": "pull_request_target",
        "display_title": "Agent Review / pr-7",
        "status": "in_progress",
        "created_at": "2026-11-05T11:00:00Z",
        "head_branch": "task",
        "repository": {"full_name": repo},
    }
    pr = {
        "number": 7,
        "state": "closed",
        "closed_at": "2026-11-05T12:00:00Z",
        "user": {"login": "owner"},
        "head": {"ref": "task", "repo": {"full_name": repo}},
        "base": {"repo": {"full_name": repo}},
    }
    (tmp_path / "fixture.json").write_text(json.dumps({"run": run, "pr": pr}))
    cli = tmp_path / "gh"
    cli.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ['FIXTURE']); data=json.loads((root/'fixture.json').read_text()); endpoint=sys.argv[2]
if 'status=' in endpoint:
    marker=root/'queued-calls'
    n=int(marker.read_text()) if marker.exists() else 0
    n+=1; marker.write_text(str(n))
    # 首个 status 查询的第一次响应漂移：count=2 但只回 1 项，之后稳定。
    if n==1:
        print(json.dumps([{'total_count':2,'workflow_runs':[data['run']]}]))
    else:
        print(json.dumps([{'total_count':1,'workflow_runs':[data['run']]}]))
elif endpoint.endswith('/cancel'):
    (root/'cancelled').write_text(endpoint)
elif '/pulls/' in endpoint:
    print(json.dumps(data['pr']))
else:
    run=data['run']
    if (root/'cancelled').exists(): run['status']='completed'
    print(json.dumps(run))
""")
    cli.chmod(0o700)
    result = subprocess.run(
        ["node", ".sandcastle/queue/retire-runs.mjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FIXTURE": str(tmp_path),
            "GH_REPO": repo,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cancelled").read_text() == "repos/owner/example/actions/runs/1/cancel"


def test_run_retirement_still_fails_closed_on_persistent_truncation(tmp_path):
    """持续不一致（真正的 API 截断）仍然拒清，fail-closed 语义不变。"""
    repo = "owner/example"
    run = {
        "id": 1,
        "path": ".github/workflows/agent-review.yml",
        "event": "pull_request_target",
        "display_title": "Agent Review / pr-7",
        "status": "in_progress",
        "created_at": "2026-11-05T11:00:00Z",
        "head_branch": "task",
        "repository": {"full_name": repo},
    }
    (tmp_path / "fixture.json").write_text(json.dumps({"run": run}))
    cli = tmp_path / "gh"
    cli.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ['FIXTURE']); data=json.loads((root/'fixture.json').read_text()); endpoint=sys.argv[2]
if 'status=' in endpoint:
    print(json.dumps([{'total_count':2,'workflow_runs':[data['run']]}]))
elif '/pulls/' in endpoint:
    print(json.dumps({}))
else:
    print(json.dumps(data['run']))
""")
    cli.chmod(0o700)
    result = subprocess.run(
        ["node", ".sandcastle/queue/retire-runs.mjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FIXTURE": str(tmp_path),
            "GH_REPO": repo,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "Run listing was truncated" in result.stderr
    assert not (tmp_path / "cancelled").exists()


def test_runner_routing_and_early_gates():
    def workflow(name):
        return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())

    cd = workflow("cd.yml")
    assert all(job["runs-on"] == "aiops-deploy" for job in cd["jobs"].values())
    assert cd["jobs"]["deploy-41"]["concurrency"]["cancel-in-progress"] is False
    for name in ["agent-implement-pr", "agent-review", "agent-update-branch"]:
        doc = workflow(name + ".yml")
        job = next(iter(doc["jobs"].values()))
        assert job["runs-on"] == "self-hosted"
        steps = job["steps"]
        assert steps[1]["id"] == "current"
        candidate = next(s for s in steps if s["name"] == "Checkout candidate PR head")
        assert "steps.current.outputs.proceed == 'true'" in candidate["if"]
        assert next(s for s in steps if s.get("id") == "deliver")["run"].endswith("queue/pr-current.mjs")
    assert workflow("agent-lifecycle.yml")["jobs"]["retire"]["runs-on"] == "ubuntu-latest"
    assert workflow("agent-promote-queued.yml")["jobs"]["promote"]["runs-on"] == "ubuntu-latest"
    assert workflow("architecture-review.yml")["jobs"]["check-backlog"]["runs-on"] == "ubuntu-latest"


def test_deployment_runner_status():
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import assert from 'node:assert/strict';
import { runnerSummary } from './.sandcastle/queue/status.mjs';
assert.equal(runnerSummary(null).kind, 'unknown');
assert.equal(runnerSummary([]).kind, 'missing');
const runner = {labels: [{name: 'aiops-deploy'}], status: 'online', busy: false};
assert.equal(runnerSummary([runner]).kind, 'available');
assert.equal(runnerSummary([{...runner, busy: undefined}]).kind, 'unknown');
assert.equal(runnerSummary([{...runner,labels:[{name:'AIOPS-DEPLOY'}]}]).kind, 'available');
assert.equal(runnerSummary([{...runner,busy:true}]).kind, 'busy');
assert.equal(runnerSummary([{...runner,status:'offline'}]).kind, 'offline');
assert.equal(runnerSummary([{...runner,labels:[...runner.labels,{name:'self-hosted'}]}]).kind, 'shared');
""",
        ],
        cwd=ROOT,
        check=True,
    )


def test_missing_runner_credential_never_closes_monitor_alarm(tmp_path):
    cli = tmp_path / "gh"
    cli.write_text("""#!/usr/bin/env python3
import json,sys
args=sys.argv[1:]
if args[0]=='run':
    print('[]' if '--status' in args else '[{"databaseId":1,"status":"completed","conclusion":"success"}]')
elif args[0]=='issue': print('[{"number":99}]')
elif args[1].endswith('/jobs'):
    print('{"total_count":1,"jobs":[{"name":"deploy-41","status":"completed","conclusion":"success"}]}')
elif 'pending_deployments' in args[1]: print('0')
else: print('[]')
""")
    cli.chmod(0o700)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("RUNNER_READ_TOKEN", None)
    result = subprocess.run(
        ["node", ".sandcastle/cd-watch.mjs", "--dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "runner-unknown" in result.stdout
    assert "Administration:read" in result.stdout
    assert "CD 告警条件不再成立" not in result.stdout
