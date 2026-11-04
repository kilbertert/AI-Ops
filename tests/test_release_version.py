"""发布版本的三个声明必须一致。

同一件事目前写在三处：`VERSION`（发布时改的那一份）、`pyproject.toml` 的
`project.version`、以及 `src/aiops_diagnostics/__init__.py` 的 `__version__`
（运行时 `/health` 读的那一份）。在此之前它们**各写各的**：`pyproject` 与
`__init__` 停在 0.1.0 而从未被推进过，注释里也直说了「与 __init__.py 本就不同步」。

两处独立写同一个事实，正是这个仓库反复记录的那种失败：它们不会同时被改，于是
某一天 `/health` 报的版本与发布 tag 说的版本不是一回事，而两边都看起来很正常。

`VERSION` 是权威：发布时由人改它。另两份是它的副本，由本测试负责让它们跟上。
"""

from __future__ import annotations

import re
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_INIT = REPO_ROOT / "src" / "aiops_diagnostics" / "__init__.py"

#: SemVer 2.0.0 的正式文法。刻意不接受 `v` 前缀与 build metadata ——
#: 这两样属于**发布动作**（tag 名、部署身份），不属于版本本身。
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _version_from_file() -> str:
    assert VERSION_FILE.is_file(), "VERSION 文件缺失；它是发布版本的权威来源"
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def test_version_file_is_a_bare_semver() -> None:
    """`VERSION` 里只能是 `MAJOR.MINOR.PATCH`。

    `v` 前缀与 `+build` 后缀是发布动作的一部分（tag 名、部署身份），写进这个文件
    会让「版本」与「这次发布」混为一谈 —— 实测 2026-10-23：给每次部署都打带
    build metadata 的 tag，`dev-changelog` 会给它们各开一节，changelog 于是变成
    「一次部署一节」，正是要避免的碎片化。
    """
    value = _version_from_file()
    assert SEMVER.match(value), f"VERSION 必须是裸 semver（MAJOR.MINOR.PATCH），得到：{value!r}"


def test_pyproject_version_matches() -> None:
    """`pyproject.toml` 的 `project.version` 必须等于 `VERSION`。

    这份是打包元数据：`uv build` 产出的 wheel 用它。它与发布版本不一致时，
    「装了什么版本的包」与「发布了什么版本」会各说各话。
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == _version_from_file(), (
        "pyproject.toml 的 project.version 与 VERSION 不一致；VERSION 是权威，改它之后同步这里"
    )


def test_runtime_version_matches() -> None:
    """`__version__` 必须等于 `VERSION`。

    这份是运行时那一份：`/health` 的 `version` 字段直接取自它，部署时由
    `deploy-41.sh` 在暂存副本上追 `+<short-sha>`。它落后时，生产自报的版本
    与实际发布的版本对不上，而两侧都不会报错。
    """
    source = PACKAGE_INIT.read_text(encoding="utf-8")
    matches = re.findall(r'^__version__ = "([^"]+)"$', source, re.M)
    assert len(matches) == 1, f"__init__.py 里应当恰好有一行 __version__，找到 {len(matches)} 行"
    assert matches[0] == _version_from_file(), (
        "__init__.py 的 __version__ 与 VERSION 不一致；VERSION 是权威，改它之后同步这里"
    )


def test_release_workflow_reads_the_version_file() -> None:
    """发布步骤必须从 `VERSION` 打 tag，而不是从别处推一个数字。

    这条钉的是「谁决定版本号」：如果 CD 自己从提交推版本，那么一次误标的 `feat!`
    就会跳大版，而没有人做过那个决定。
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "cd.yml").read_text(encoding="utf-8")
    assert "VERSION" in workflow, "cd.yml 没有读 VERSION"
    assert "v${" in workflow or "v$version" in workflow, "cd.yml 没有用 VERSION 的值打 v* tag"


@pytest.mark.parametrize(
    "existing", [None, "annotated", "lightweight", "race_same", "race_other", "race_lightweight"]
)
def test_release_tag_creation_preserves_existing_versions(tmp_path, existing):
    """执行CD真实标签片段：新标签有注释，旧标签不移动，并发冲突不能假成功。"""
    import os
    import subprocess

    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Release test",
        "GIT_AUTHOR_EMAIL": "release@example.test",
        "GIT_COMMITTER_NAME": "Release test",
        "GIT_COMMITTER_EMAIL": "release@example.test",
    }

    def git(*args, cwd=remote, text=""):
        return subprocess.check_output(["git", *args], cwd=cwd, env=env, input=text, text=True).strip()

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True
    )
    tree = git("mktree")
    first = git("commit-tree", tree, text="first\n")
    target = git("commit-tree", tree, "-p", first, text="release\n")
    git("update-ref", "refs/heads/main", target)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    if existing in {"annotated", "lightweight"}:
        if existing == "annotated":
            git("tag", "-a", "v1.2.3", first, "-m", "previous release")
        else:
            git("update-ref", "refs/tags/v1.2.3", first)
    before = git("rev-parse", "refs/tags/v1.2.3") if existing in {"annotated", "lightweight"} else None
    cli = tmp_path / "gh"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        + """import os, subprocess, sys
args = sys.argv[1:]
fields = dict(args[i+1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")
remote = os.environ["TEST_REMOTE"]
def git(*argv, text=""):
    return subprocess.check_output(["git", "--git-dir="+remote, *argv], input=text, text=True).strip()
if args[1].endswith("/git/tags"):
    data = ("object " + fields["object"] + "\\ntype commit\\ntag " + fields["tag"]
            + "\\ntagger Release test <release@example.test> 1 +0000\\n\\n" + fields["message"] + "\\n")
    print(git("mktag", text=data))
else:
    race = os.environ["TEST_EXISTING"]
    if race.startswith("race_"):
        oid = fields["sha"] if race == "race_same" else os.environ["TEST_TARGET"]
        if race == "race_other": oid = os.environ["TEST_FIRST"]
        git("update-ref", fields["ref"], oid)
        sys.exit(1)
    git("update-ref", fields["ref"], fields["sha"], "0" * 40)
"""
    )
    cli.chmod(0o700)
    workflow = (REPO_ROOT / ".github/workflows/cd.yml").read_text()
    start = workflow.index('          vtag="v${version}"')
    end = workflow.index("          notes=$(mktemp)", start)
    script = "set -eu\nversion=1.2.3\n" + textwrap.dedent(workflow[start:end])
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        env={
            **env,
            "PATH": f"{tmp_path}:{env['PATH']}",
            "TARGET_SHA": target,
            "GITHUB_REPOSITORY": "example/repo",
            "TEST_REMOTE": str(remote),
            "TEST_EXISTING": existing or "",
            "TEST_TARGET": target,
            "TEST_FIRST": first,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) == (existing not in {"race_other", "race_lightweight"}), result.stderr
    if before:
        assert git("rev-parse", "refs/tags/v1.2.3") == before
    elif result.returncode == 0:
        assert git("cat-file", "-t", "v1.2.3") == "tag"
        assert git("rev-parse", "v1.2.3^{commit}") == target
