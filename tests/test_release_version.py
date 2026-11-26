"""Release metadata and explicitly selected main-batch workflow tests."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_INIT = REPO_ROOT / "src" / "aiops_diagnostics" / "__init__.py"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _version_from_file() -> str:
    assert VERSION_FILE.is_file(), "VERSION 文件缺失；它是发布版本的权威来源"
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _workflow():
    import yaml

    return yaml.load((REPO_ROOT / ".github/workflows/cd.yml").read_text(), Loader=yaml.BaseLoader)


def _step(workflow, job: str, name: str) -> dict:
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)


def _git(args: list[str], cwd: Path, env: dict[str, str], *, input: str | None = None) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, env=env, input=input, text=True).strip()


def _run_script(script: str, cwd: Path, env: dict[str, str]):
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", textwrap.dedent(script)],
        cwd=cwd,
        env={**env, "BASH_ENV": ""},
        capture_output=True,
        text=True,
        timeout=20,
    )


def _release_git_env() -> dict[str, str]:
    return {
        **{key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ},
        "GIT_AUTHOR_NAME": "Release test",
        "GIT_AUTHOR_EMAIL": "release@example.test",
        "GIT_COMMITTER_NAME": "Release test",
        "GIT_COMMITTER_EMAIL": "release@example.test",
    }


def test_version_file_is_a_bare_semver() -> None:
    value = _version_from_file()
    assert SEMVER.match(value), f"VERSION 必须是裸 semver（MAJOR.MINOR.PATCH），得到：{value!r}"


def test_pyproject_version_matches() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == _version_from_file(), (
        "pyproject.toml 的 project.version 与 VERSION 不一致；VERSION 是权威，改它之后同步这里"
    )


def test_runtime_version_matches() -> None:
    source = PACKAGE_INIT.read_text(encoding="utf-8")
    matches = re.findall(r'^__version__ = "([^"]+)"$', source, re.M)
    assert len(matches) == 1, f"__init__.py 里应当恰好有一行 __version__，找到 {len(matches)} 行"
    assert matches[0] == _version_from_file(), (
        "__init__.py 的 __version__ 与 VERSION 不一致；VERSION 是权威，改它之后同步这里"
    )


def test_release_workflow_is_explicit_and_serialized() -> None:
    workflow = _workflow()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["concurrency"] == {"group": "aiops-production-release", "cancel-in-progress": "false"}
    assert workflow["permissions"]["contents"] == "read"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["target_sha"]["required"] == "true"
    assert workflow["jobs"]["deploy-41"]["concurrency"]["group"] == "cd-41-deploy"
    assert workflow["jobs"]["deploy-41"]["concurrency"]["cancel-in-progress"] == "false"
    assert "concurrency" not in workflow["jobs"]["release-notes"]
    deploy = workflow["jobs"]["deploy-41"]
    assert deploy["if"] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
    assert deploy["environment"] == "production-41"
    selected = _step(workflow, "deploy-41", "Check out the selected deployment commit")
    assert selected["with"]["ref"] == "${{ steps.target.outputs.sha }}"
    assert selected["with"]["persist-credentials"] == "false"
    assert workflow["jobs"]["release-notes"]["permissions"]["contents"] == "write"


def test_release_resolves_only_full_main_sha_and_its_version(tmp_path: Path) -> None:
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    env = _release_git_env()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True
    )
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    _git(["config", "user.name", "Release test"], checkout, env)
    _git(["config", "user.email", "release@example.test"], checkout, env)
    (checkout / "VERSION").write_text("1.2.3\n")
    _git(["add", "VERSION"], checkout, env)
    _git(["commit", "-m", "chore(release): prepare 1.2.3"], checkout, env)
    _git(["push", "origin", "main"], checkout, env)
    main_sha = _git(["rev-parse", "HEAD"], checkout, env)
    (checkout / "VERSION").write_text("1.2.4\n")
    _git(["add", "VERSION"], checkout, env)
    _git(["commit", "-m", "chore(release): prepare 1.2.4"], checkout, env)
    _git(["push", "origin", "HEAD:refs/heads/candidate"], checkout, env)
    candidate_sha = _git(["rev-parse", "HEAD"], checkout, env)

    script = _step(_workflow(), "deploy-41", "Resolve the commit to deploy")["run"]
    output = tmp_path / "github-output"
    good = _run_script(script, checkout, {**env, "TARGET_INPUT": main_sha, "GITHUB_OUTPUT": str(output)})
    assert good.returncode == 0, good.stderr
    assert f"sha={main_sha}" in output.read_text()
    assert "version=1.2.3" in output.read_text()

    not_full = _run_script(
        script, checkout, {**env, "TARGET_INPUT": main_sha[:12], "GITHUB_OUTPUT": str(output)}
    )
    assert not_full.returncode != 0 and "完整 SHA-1" in not_full.stderr
    not_on_main = _run_script(
        script, checkout, {**env, "TARGET_INPUT": candidate_sha, "GITHUB_OUTPUT": str(output)}
    )
    assert not_on_main.returncode != 0 and "不在 origin/main 历史" in not_on_main.stderr

    (checkout / "VERSION").write_text("1.02.4\n")
    _git(["add", "VERSION"], checkout, env)
    _git(["commit", "-m", "chore(release): malformed version fixture"], checkout, env)
    _git(["push", "origin", "main"], checkout, env)
    malformed = _git(["rev-parse", "HEAD"], checkout, env)
    refused = _run_script(script, checkout, {**env, "TARGET_INPUT": malformed, "GITHUB_OUTPUT": str(output)})
    assert refused.returncode != 0 and "不是 SemVer" in refused.stderr


@pytest.mark.parametrize("case", ["new", "existing", "mismatch", "lightweight", "race_same", "race_other"])
def test_version_tag_reservation_is_annotated_immutable_and_race_safe(tmp_path: Path, case: str) -> None:
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    env = _release_git_env()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True
    )
    empty_tree = _git(["mktree"], remote, env)
    previous = _git(["commit-tree", empty_tree], remote, env, input="previous\n")
    target = _git(["commit-tree", empty_tree, "-p", previous], remote, env, input="release\n")
    _git(["update-ref", "refs/heads/main", target], remote, env)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    if case in {"existing", "mismatch"}:
        _git(
            ["tag", "-a", "v1.2.3", target if case == "existing" else previous, "-m", "existing"], remote, env
        )
    elif case == "lightweight":
        _git(["update-ref", "refs/tags/v1.2.3", target], remote, env)
    before = (
        _git(["rev-parse", "v1.2.3"], remote, env)
        if case in {"existing", "mismatch", "lightweight"}
        else None
    )

    cli = tmp_path / "gh"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        + """import os, subprocess, sys
args = sys.argv[1:]
fields = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")
remote = os.environ["TEST_REMOTE"]
tag_object_file = os.environ["TEST_TAG_OBJECT_FILE"]
def git(*argv, text=""):
    return subprocess.check_output(["git", "--git-dir=" + remote, *argv], input=text, text=True).strip()
if args[1].endswith("/git/tags"):
    data = ("object " + fields["object"] + "\\ntype commit\\ntag " + fields["tag"]
            + "\\ntagger Release test <release@example.test> 1 +0000\\n\\n" + fields["message"] + "\\n")
    tag_object = git("mktag", text=data)
    open(tag_object_file, "w").write(tag_object)
    print(tag_object)
else:
    mode = os.environ["TEST_CASE"]
    if mode.startswith("race_"):
        tag = open(tag_object_file).read().strip() if mode == "race_same" else git(
            "mktag", text=("object " + os.environ["TEST_PREVIOUS"] + "\\ntype commit\\ntag v1.2.3\\n"
            "tagger Release test <release@example.test> 1 +0000\\n\\nraced\\n"))
        git("update-ref", fields["ref"], tag)
        sys.exit(1)
    git("update-ref", fields["ref"], fields["sha"], "0" * 40)
"""
    )
    cli.chmod(0o700)
    script = _step(_workflow(), "deploy-41", "Reserve or verify the immutable version tag")["run"]
    result = _run_script(
        script,
        checkout,
        {
            **env,
            "PATH": f"{tmp_path}:{env['PATH']}",
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "example/repo",
            "TARGET_SHA": target,
            "VERSION": "1.2.3",
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
            "TEST_REMOTE": str(remote),
            "TEST_TAG_OBJECT_FILE": str(tmp_path / "tag-object"),
            "TEST_CASE": case,
            "TEST_PREVIOUS": previous,
        },
    )
    should_succeed = case in {"new", "existing", "race_same"}
    assert (result.returncode == 0) == should_succeed, result.stderr
    if before:
        assert _git(["rev-parse", "v1.2.3"], remote, env) == before
    if should_succeed:
        assert _git(["cat-file", "-t", "refs/tags/v1.2.3"], remote, env) == "tag"
        assert _git(["rev-parse", "refs/tags/v1.2.3^{commit}"], remote, env) == target


@pytest.mark.parametrize("case", ["create", "edit", "missing_section", "mismatch", "unrelated_tag"])
def test_release_notes_publish_only_selected_version_section(tmp_path: Path, case: str) -> None:
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    env = _release_git_env()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)
    (repo / "VERSION").write_text("1.2.3\n")
    _git(["add", "VERSION"], repo, env)
    _git(["commit", "-m", "feat(example): selected release batch"], repo, env)
    target = _git(["rev-parse", "HEAD"], repo, env)
    _git(["tag", "-a", "v1.2.3", "-m", "selected release"], repo, env)
    _git(["remote", "add", "origin", str(remote)], repo, env)
    _git(["push", "origin", "main", "--tags"], repo, env)
    if case == "unrelated_tag":
        # A later release may have been reserved while re-running notes for an older batch.
        _git(["commit", "--allow-empty", "-m", "feat(example): later batch"], repo, env)
        _git(["tag", "-a", "v1.2.4", "-m", "later release"], repo, env)
        _git(["push", "origin", "main", "--tags"], repo, env)
        _git(["checkout", "--detach", target], repo, env)
    dev_changelog = tmp_path / "dev-changelog"
    heading = "9.9.9" if case == "missing_section" else "1.2.3"
    dev_changelog.write_text(
        "#!/bin/sh\ncat > \"$1/CHANGELOG.md\" <<'EOF'\n"
        f"# Changelog\n\n## [Unreleased]\n\n## [{heading}] - 2026-11-26\n\n### Added\n"
        "- Ship the selected batch\n\n"
        "## [1.2.2] - 2026-10-01\n\n### Fixed\n- Older content\nEOF\n"
    )
    dev_changelog.chmod(0o700)
    call = tmp_path / "gh-call"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = view ]; then exit "$TEST_RELEASE_EXISTS"; fi\n'
        'printf \'%s\\n\' "$*" > "$TEST_CALL"\n'
        'while [ $# -gt 0 ]; do if [ "$1" = --notes-file ]; then cat "$2" >> "$TEST_CALL"; fi; shift; done\n'
    )
    gh.chmod(0o700)
    run = _step(_workflow(), "release-notes", "Generate and publish release notes")["run"]
    result = _run_script(
        run,
        repo,
        {
            **env,
            "PATH": f"{tmp_path}:{env['PATH']}",
            "GH_TOKEN": "test-token",
            "TARGET_SHA": "a" * 40 if case == "mismatch" else target,
            "VERSION": "1.2.3",
            "VERSION_TAG": "v1.2.3",
            "TEST_RELEASE_EXISTS": "0" if case == "edit" else "1",
            "TEST_CALL": str(call),
        },
    )
    if case in {"missing_section", "mismatch"}:
        assert result.returncode != 0
        assert not call.exists()
    else:
        assert result.returncode == 0, result.stderr
        command = call.read_text()
        assert ("release edit v1.2.3" if case == "edit" else "release create v1.2.3") in command
        assert ("--verify-tag" in command) if case != "edit" else ("--prerelease=false" in command)
        assert "Ship the selected batch" in command
        assert "Unreleased" not in command and "Older content" not in command
