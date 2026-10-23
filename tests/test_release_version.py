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
import tomllib
from pathlib import Path

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
