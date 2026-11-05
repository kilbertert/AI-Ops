"""交付自动化的行为边界：候选竞争与 pytest 的失败信号。"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow_step(workflow, name):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    return next(
        step for job in document["jobs"].values() for step in job["steps"] if step.get("name") == name
    )["run"]


def test_result_guard_uses_junit_outcomes(tmp_path):
    spec = importlib.util.spec_from_file_location("ci_results", ROOT / "tools/verification/ci_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "pytest.xml"
    report.write_text('<testsuites><testsuite><testcase name="ok"/></testsuite></testsuites>')
    assert module.check_results(report, 1) == "passed=1 skipped=0 failed=0"
    for cases in ("", "<testcase><failure/></testcase>", "<testcase><error/></testcase>"):
        report.write_text(f"<testsuite>{cases}</testsuite>")
        with pytest.raises(ValueError):
            module.check_results(report, 1)
    report.write_text("<testsuite><testcase/>" + "<testcase><skipped/></testcase>" * 11 + "</testsuite>")
    with pytest.raises(ValueError, match="skipped=11"):
        module.check_results(report, 1)
    report.write_text("not XML")
    with pytest.raises(ElementTree.ParseError):
        module.check_results(report, 1)


def candidate(number, **changes):
    return {
        "number": number,
        "state": "OPEN",
        "baseRefName": "main",
        "isDraft": False,
        "mergeStateStatus": "BEHIND",
        "author": {"login": "owner"},
        "isCrossRepository": False,
        "assignees": [],
        "labels": [],
        "headRefOid": f"head-{number}",
        **changes,
    }


def sync(tmp_path, current, api_exit=0):
    refreshed = tmp_path / "current.json"
    refreshed.write_text(json.dumps(current))
    calls = tmp_path / "calls.jsonl"
    cli = tmp_path / "gh"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'view']: print(Path(os.environ['CURRENT']).read_text())\n"
        "elif args[0] == 'api':\n"
        "    with Path(os.environ['CALLS']).open('a') as out: out.write(json.dumps(args) + '\\n')\n"
        "    sys.exit(int(os.environ['API_EXIT']))\n"
        "else: sys.exit(99)\n"
    )
    cli.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", workflow_step("sync-open-prs.yml", "Update one delivery candidate")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GH_REPO": "owner/example",
            "REPOSITORY_OWNER": "owner",
            "PR_NUMBER": "1",
            "CURRENT": str(refreshed),
            "CALLS": str(calls),
            "API_EXIT": str(api_exit),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result, [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def test_sync_updates_only_one_candidate_with_expected_head(tmp_path):
    result, calls = sync(tmp_path, candidate(1))
    assert result.returncode == 0, result.stderr
    assert calls == [
        [
            "api",
            "--method",
            "PUT",
            "repos/owner/example/pulls/1/update-branch",
            "-f",
            "expected_head_sha=head-1",
        ]
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "CLOSED"},
        {"baseRefName": "develop"},
        {"isDraft": True},
        {"isCrossRepository": True},
        {"author": {"login": "someone-else"}},
        {"assignees": [{"login": "owner"}]},
        {"labels": [{"name": "agent:in-progress"}]},
        {"labels": [{"name": "agent:update-branch"}]},
        {"labels": [{"name": "agent:blocked"}]},
        {"mergeStateStatus": "DIRTY"},
    ],
)
def test_sync_leaves_unapproved_or_owned_work_untouched(tmp_path, changes):
    result, calls = sync(tmp_path, candidate(1, **changes))
    assert result.returncode != 0
    assert calls == []


def test_sync_does_not_hide_a_rejected_update(tmp_path):
    result, calls = sync(tmp_path, candidate(1), api_exit=1)
    assert result.returncode != 0
    assert len(calls) == 1


@pytest.mark.parametrize("deploy,release", [("success", "failure"), ("failure", "skipped"), (None, None)])
def test_cd_watch_reads_deployment_not_release_result(tmp_path, deploy, release):
    cli = tmp_path / "gh"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"deploy, release = {deploy!r}, {release!r}\n"
        "if sys.argv[1] == 'run':\n"
        "    print(json.dumps([{'databaseId': 1, 'status': 'in_progress', 'conclusion': ''}]))\n"
        "elif sys.argv[2].endswith('/jobs'):\n"
        "    print(json.dumps({'total_count': 2, 'jobs': [\n"
        "        {'name': 'deploy-41', 'conclusion': deploy},\n"
        "        {'name': 'release-notes', 'conclusion': release}]}))\n"
        "else: print('0')\n"
    )
    cli.chmod(0o700)
    script = (
        f"import {{fetchRuns}} from {json.dumps((ROOT / '.sandcastle/cd-watch.mjs').as_uri())};"
        "console.log(JSON.stringify(fetchRuns().runs[0]));"
    )
    output = subprocess.check_output(
        ["node", "--input-type=module", "-e", script],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        text=True,
    )
    run = json.loads(output)
    assert run["conclusion"] == (deploy or "")
    assert run["status"] == ("completed" if deploy else "in_progress")


def test_release_job_is_bound_to_successful_deployment():
    jobs = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())["jobs"]
    release = jobs["release-notes"]
    assert release["needs"] == "deploy-41"
    assert "if" not in release  # 默认 success()，失败部署不能发布
    assert "environment" not in release  # 发布作业不产生虚假的生产部署账本
    assert release["steps"][0]["with"]["ref"] == "${{ needs.deploy-41.outputs.sha }}"
    assert jobs["deploy-41"]["concurrency"]["cancel-in-progress"] is False


def test_automatic_sync_events_only_report():
    jobs = yaml.safe_load((ROOT / ".github/workflows/sync-open-prs.yml").read_text())["jobs"]
    step = next(s for s in jobs["sync"]["steps"] if s["name"] == "Update one delivery candidate")
    assert step["if"] == "github.event_name == 'workflow_dispatch'"


def test_empty_document_selection_propagates_full_tier(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("# no document references\n")
    cli = tmp_path / "uv"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-c", workflow_step("ci.yml", "Run tests")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TEST_TIER": "docs",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
        capture_output=True,
    )
    assert output.read_text().strip() == "tier=full"
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["verify"]["steps"]
    build = next(s for s in steps if s.get("name") == "Build and inspect Gateway image")
    assert build["if"] == "steps.tests.outputs.tier != 'docs'"
