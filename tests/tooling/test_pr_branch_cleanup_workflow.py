"""闭环校验未合入关闭 PR 分支自动清理与 GitLab 镜像周期兜底工作流（ADR-0364）。"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cleanup-closed-pr-branch.yml"
MIRROR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gitlab-mirror.yml"


def _load_yaml(path: Path) -> dict:
    # PyYAML 1.1 treats bare `on` key as boolean True.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cleanup_script() -> str:
    data = _load_yaml(CLEANUP_WORKFLOW)
    steps = data["jobs"]["delete-branch"]["steps"]
    return textwrap.dedent(steps[0]["run"])


def test_cleanup_workflow_shape_permissions_and_guards() -> None:
    data = _load_yaml(CLEANUP_WORKFLOW)
    on_cfg = data.get("on", data.get(True))
    assert on_cfg == {"pull_request_target": {"types": ["closed"]}}
    assert data["permissions"] == {"actions": "write", "contents": "write", "pull-requests": "read"}

    job = data["jobs"]["delete-branch"]
    condition = " ".join(str(job["if"]).split())
    assert "github.event.pull_request.merged == false" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    for step in job["steps"]:
        assert "actions/checkout" not in str(step.get("uses", ""))
        assert "${{" not in str(step.get("run", ""))


def test_gitlab_mirror_workflow_keeps_push_delete_and_weekly_schedule() -> None:
    data = _load_yaml(MIRROR_WORKFLOW)
    on_cfg = data.get("on", data.get(True))
    assert "push" in on_cfg
    assert "delete" in on_cfg
    assert "workflow_dispatch" in on_cfg
    assert on_cfg["schedule"] == [{"cron": "15 3 * * 0"}]


def _run_cleanup(
    tmp_path: Path,
    *,
    head_ref: str,
    head_sha: str = "abc123",
    open_pulls_json: str = "[]",
    ref_sha: str | None = "abc123",
    default_branch: str = "main",
    delete_status: int = 0,
    dispatch_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls_log = tmp_path / "gh_calls.log"
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "open_pulls.json").write_text(open_pulls_json, encoding="utf-8")
    if ref_sha is not None:
        (state_dir / "ref_sha.txt").write_text(ref_sha, encoding="utf-8")

    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env python3
import json, pathlib, sys
log = pathlib.Path({str(calls_log)!r})
state = pathlib.Path({str(state_dir)!r})
args = sys.argv[1:]
with log.open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
if args[:3] == ["api", "-X", "DELETE"]:
    sys.exit({delete_status!r})
if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/gitlab-mirror.yml/dispatches"):
    sys.exit({dispatch_status!r})
endpoint = args[1]
if "/pulls?" in endpoint:
    data = json.loads((state / "open_pulls.json").read_text(encoding="utf-8"))
    print(len(data))
    sys.exit(0)
if "/git/ref/heads/" in endpoint:
    ref_file = state / "ref_sha.txt"
    if not ref_file.exists():
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    print(ref_file.read_text(encoding="utf-8").strip())
    sys.exit(0)
sys.exit(2)
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GH_TOKEN": "test-token",
        "REPO": "kilbertert/AI-Ops",
        "OWNER": "kilbertert",
        "DEFAULT_BRANCH": default_branch,
        "HEAD_REF": head_ref,
        "HEAD_SHA": head_sha,
    }
    proc = subprocess.run(
        ["bash", "-c", _cleanup_script()],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
    return proc, calls


@pytest.mark.parametrize("protected", ["main", "master", "release/1.0", "standby/local-gateway-cold", ""])
def test_refuses_protected_and_empty_branches(tmp_path: Path, protected: str) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref=protected)
    assert proc.returncode == 0, proc.stderr
    assert calls == []


def test_skips_when_another_open_pr_uses_branch(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/sub-task", open_pulls_json='[{"number": 1808}]')
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith(("api -X DELETE", "api -X POST")) for c in calls)


def test_exits_cleanly_when_branch_already_absent(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/gone", ref_sha=None)
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith(("api -X DELETE", "api -X POST")) for c in calls)


def test_skips_when_branch_tip_moved_since_close(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/reused", head_sha="old111", ref_sha="new222")
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith(("api -X DELETE", "api -X POST")) for c in calls)


def test_dispatches_mirror_on_default_branch_after_successful_delete(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/sub-task", default_branch="trunk")
    assert proc.returncode == 0, proc.stderr
    assert calls[2:] == [
        "api -X DELETE repos/kilbertert/AI-Ops/git/refs/heads/feat/sub-task",
        "api -X POST repos/kilbertert/AI-Ops/actions/workflows/gitlab-mirror.yml/dispatches "
        "-f ref=trunk --silent",
    ]


def test_does_not_dispatch_when_delete_fails(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/sub-task", delete_status=1)
    assert proc.returncode != 0
    assert calls[2:] == ["api -X DELETE repos/kilbertert/AI-Ops/git/refs/heads/feat/sub-task"]


def test_fails_visibly_when_mirror_dispatch_fails_after_delete(tmp_path: Path) -> None:
    proc, calls = _run_cleanup(tmp_path, head_ref="feat/sub-task", dispatch_status=1)
    assert proc.returncode != 0
    assert "GitHub branch deleted, but GitLab mirror dispatch failed" in proc.stderr
    assert len(calls) == 4
    assert calls[-1].endswith("/gitlab-mirror.yml/dispatches -f ref=main --silent")


def test_safe_against_shell_characters_in_branch_name(tmp_path: Path) -> None:
    pwn = tmp_path / "pwned"
    weird = f"feat/$(touch {pwn})"
    proc, calls = _run_cleanup(tmp_path, head_ref=weird, head_sha="abc123", ref_sha="abc123")
    assert proc.returncode == 0, proc.stderr
    assert not pwn.exists()
    assert any("-X DELETE" in c for c in calls)
