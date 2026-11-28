"""交付自动化的行为边界：CD Watch、sync-open-prs 薄壳委托与 pytest 失败信号。"""

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


@pytest.mark.parametrize(
    "deploy,release",
    [("success", "failure"), ("failure", "skipped"), ("success", "success"), (None, None)],
)
def test_cd_watch_preserves_workflow_and_deployment_results(tmp_path, deploy, release):
    cli = tmp_path / "gh"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"deploy, release = {deploy!r}, {release!r}\n"
        "conclusion = 'success' if deploy == release == 'success' else 'failure' if release else None\n"
        "if sys.argv[1] == 'run':\n"
        "    print(json.dumps([{'databaseId': 1, 'status': 'completed' if release else 'in_progress', "
        "'conclusion': conclusion}]))\n"
        "elif sys.argv[2].endswith('/jobs'):\n"
        "    print(json.dumps({'total_count': 2, 'jobs': [\n"
        "        {'name': 'deploy-41', 'status': 'completed' if deploy else 'in_progress', "
        "'conclusion': deploy},\n"
        "        {'name': 'release-notes', 'conclusion': release}]}))\n"
        "else: print('0')\n"
    )
    cli.chmod(0o700)
    script = (
        f"import {{fetchRuns}} from {json.dumps((ROOT / 'tools/verification/cd-watch.mjs').as_uri())};"
        "console.log(JSON.stringify(fetchRuns().runs[0]));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    run = json.loads(result.stdout)
    assert run["conclusion"] == (
        "success" if deploy == release == "success" else "failure" if release else None
    )
    assert run["status"] == ("completed" if release else "in_progress")
    assert run["deploymentConclusion"] == deploy


def test_release_job_is_bound_to_successful_deployment():
    jobs = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())["jobs"]
    release = jobs["release-notes"]
    assert release["needs"] == "deploy-41"
    assert "if" not in release  # 默认 success()，失败部署不能发布
    assert "environment" not in release  # 发布作业不产生虚假的生产部署账本
    checkout = next(s for s in release["steps"] if s.get("name") == "Check out the selected release commit")
    assert checkout["with"]["ref"] == "${{ needs.deploy-41.outputs.sha }}"
    workflow = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    assert workflow["concurrency"]["group"] == "aiops-production-release"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert jobs["deploy-41"]["concurrency"]["cancel-in-progress"] is False


def test_automatic_sync_events_only_report_via_fleet_kit():
    doc = yaml.safe_load((ROOT / ".github/workflows/sync-open-prs.yml").read_text())
    job = doc["jobs"]["sync"]
    assert job["uses"] == "kilbertert/fleet-kit/.github/workflows/sync-open-prs.yml@v1"
    assert "github.event_name == 'workflow_dispatch'" in str(job["with"]["pr_number"])
    assert job["secrets"] == "inherit"


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
