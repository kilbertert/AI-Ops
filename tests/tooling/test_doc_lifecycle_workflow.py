"""验证文档自动减债 GitHub Actions 工作流（.github/workflows/doc-lifecycle.yml，ADR-0371）。"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "doc-lifecycle.yml"


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _sync_issue_script() -> str:
    data = _load_workflow()
    steps = data["jobs"]["weekly-autovacuum"]["steps"]
    step = next(s for s in steps if s["name"] == "Sync rolling maintenance Issue")
    return textwrap.dedent(step["run"])


def test_doc_lifecycle_workflow_shape_permissions_and_pinned_actions() -> None:
    data = _load_workflow()
    on_cfg = data.get("on", data.get(True))
    assert "pull_request" in on_cfg
    assert "workflow_dispatch" in on_cfg
    assert on_cfg["schedule"] == [{"cron": "30 3 * * 0"}]
    assert data["permissions"] == {}

    pr_job = data["jobs"]["pr-check"]
    assert pr_job["permissions"] == {"contents": "read"}
    weekly_job = data["jobs"]["weekly-autovacuum"]
    assert weekly_job["permissions"] == {"contents": "read", "issues": "write"}

    for job in (pr_job, weekly_job):
        for step in job["steps"]:
            uses = str(step.get("uses", ""))
            if uses:
                assert re.search(r"@[0-9a-f]{40}$", uses), f"Action must be SHA-pinned: {uses}"
            assert "${{" not in str(step.get("run", "")), "Avoid expression injection inside run scripts"


def _run_sync_step(
    tmp_path: Path,
    *,
    has_findings: bool,
    existing_issues: list[dict],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls_log = tmp_path / "gh_calls.log"
    state_file = tmp_path / "issues.json"
    state_file.write_text(json.dumps(existing_issues), encoding="utf-8")

    audit_json = tmp_path / "doc-audit.json"
    audit_json.write_text(json.dumps({"has_actionable_findings": has_findings}), encoding="utf-8")
    audit_md = tmp_path / "doc-audit.md"
    audit_md.write_text("<!-- aiops-doc-lifecycle-ledger -->\n## Report\n", encoding="utf-8")

    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env python3
import json, pathlib, sys
log = pathlib.Path({str(calls_log)!r})
state = pathlib.Path({str(state_file)!r})
args = sys.argv[1:]
with log.open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
if args[0] == "label":
    sys.exit(0)
if args[0] == "api" and args[1].startswith("repos/") and "/issues?" in args[1]:
    issues = json.loads(state.read_text(encoding="utf-8"))
    marker = "<!-- aiops-doc-lifecycle-ledger -->"
    matched = [i["number"] for i in issues if marker in (i.get("body") or "")]
    if matched:
        print(matched[0])
    sys.exit(0)
if args[0] == "api" and args[1] == "-X":
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
        "AUDIT_JSON": str(audit_json),
        "AUDIT_MD": str(audit_md),
    }
    proc = subprocess.run(["bash", "-c", _sync_issue_script()], env=env, capture_output=True, text=True)
    calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
    return proc, calls


def test_sync_creates_issue_when_findings_and_none_open(tmp_path: Path) -> None:
    proc, calls = _run_sync_step(tmp_path, has_findings=True, existing_issues=[])
    assert proc.returncode == 0, proc.stderr
    assert any("api -X POST repos/kilbertert/AI-Ops/issues" in c for c in calls), calls


def test_sync_updates_existing_rolling_issue_without_duplicating(tmp_path: Path) -> None:
    existing = [{"number": 9001, "body": "<!-- aiops-doc-lifecycle-ledger -->\nold"}]
    proc, calls = _run_sync_step(tmp_path, has_findings=True, existing_issues=existing)
    assert proc.returncode == 0, proc.stderr
    assert any("api -X PATCH repos/kilbertert/AI-Ops/issues/9001" in c for c in calls), calls
    assert not any("api -X POST repos/kilbertert/AI-Ops/issues " in c for c in calls), calls


def test_sync_closes_existing_issue_when_clean(tmp_path: Path) -> None:
    existing = [{"number": 9002, "body": "<!-- aiops-doc-lifecycle-ledger -->\nold"}]
    proc, calls = _run_sync_step(tmp_path, has_findings=False, existing_issues=existing)
    assert proc.returncode == 0, proc.stderr
    assert any("api -X POST repos/kilbertert/AI-Ops/issues/9002/comments" in c for c in calls), calls
    assert any("api -X PATCH repos/kilbertert/AI-Ops/issues/9002 -f state=closed" in c for c in calls), calls


def test_sync_is_noop_when_clean_and_no_open_issue(tmp_path: Path) -> None:
    proc, calls = _run_sync_step(tmp_path, has_findings=False, existing_issues=[])
    assert proc.returncode == 0, proc.stderr
    assert not any("-X POST" in c or "-X PATCH" in c for c in calls), calls
