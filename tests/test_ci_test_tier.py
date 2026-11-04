"""CI 的测试分档判定：**默认全量，只有改动全在文档里才降级**。

`ci.yml` 的 `Decide the test tier` 一步决定这次 PR 跑全量（4114 条 / 192 秒）
还是只跑读文档的那批（683 条 / 26 秒）。判定本身是一段 `git diff --name-only`
加一个 `grep -vE`，它不会自己报错 —— 判断写宽了就是把改代码的提交当文档提交放过，
而**看起来完全正常**。

所以这里驱动 workflow 里那段脚本本身（不是它的副本），喂进不同的改动集合，
断言它算出哪一档。脚本从文件里抽出来执行，改脚本即改判据。

⚠️ 抽原文而不是复制逻辑，理由与 `test_release_version.py` 对 `cd.yml` 的做法一致：
复制一份"测试里的判据"会和 CI 里那份各自漂移，而漂移的那天两边都显示绿。
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _tier_script() -> str:
    """`Decide the test tier` 一步的 `run:` 正文，逐字取出。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "- name: Decide the test tier")
        run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    except StopIteration:
        raise AssertionError("ci.yml 里找不到 `Decide the test tier` 步骤（改名或改形状了？）") from None
    indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    body: list[str] = []
    for line in lines[run_at + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body.append(line)
    script = textwrap.dedent("\n".join(body))
    assert "GITHUB_OUTPUT" in script, "抽出来的不是那段判定脚本"
    return script


def _decide(tmp_path: Path, changed: list[str], *, base: str = "aaa", head: str = "bbb") -> str:
    """跑 workflow 里那段脚本，返回它写出的 tier（或 `""` 表示没跑判定）。"""
    script = _tier_script().replace("${{ github.event.pull_request.base.sha }}", base)
    script = script.replace("${{ github.event.pull_request.head.sha }}", head)
    # 脚本用 `git diff --name-only` 取改动；用一个假 git 顶替，输出就是喂进去的清单。
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    payload = tmp_path / "changed.txt"
    payload.write_text("\n".join(changed) + ("\n" if changed else ""), encoding="utf-8")
    fake_git = fake_bin / "git"
    fake_git.write_text(f'#!/bin/sh\ncat "{payload}"\n', encoding="utf-8")
    fake_git.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"^tier=(\w+)$", output.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.parametrize(
    "changed",
    [
        ["docs/validation/2026-11-04-x.md"],
        ["docs/agents/env-41-runbook.md", "docs/开发进度/2026-11-04-x.md"],
        ["README.md"],
        ["docs/a.md", "CHANGELOG.md", "SOP.md"],
    ],
)
def test_documentation_only_changes_take_the_docs_tier(tmp_path: Path, changed: list[str]) -> None:
    assert _decide(tmp_path, changed) == "docs"


@pytest.mark.parametrize(
    "changed",
    [
        ["src/aiops_diagnostics/gateway/gateway_api.py"],
        ["tests/dify/test_dual_entry.py"],  # 改测试更要验证测试
        ["docs/a.md", "src/aiops_diagnostics/x.py"],  # 混合：只要有一个非文档就全量
        ["pyproject.toml"],
        ["ops/nginx/operator-h5.conf"],
        ["deploy/m2-live-03/checks.sh"],
        ["docsomething.py"],  # `docs` 前缀但不带斜杠，不是文档目录
        ["sub/docs/a.md"],  # 只有**根层** docs/ 才算
        ["sub/README.md"],  # 只有**根层** .md 才算
    ],
)
def test_anything_off_the_whitelist_takes_the_full_tier(tmp_path: Path, changed: list[str]) -> None:
    assert _decide(tmp_path, changed) == "full"


def test_an_empty_diff_falls_back_to_full(tmp_path: Path) -> None:
    """取不到改动时不能猜成"没改代码"，那正是静默少测的形状。"""
    assert _decide(tmp_path, []) == "full"


def test_a_missing_pr_base_falls_back_to_full(tmp_path: Path) -> None:
    """非 PR 触发（没有 base/head）时判据必须走全量。"""
    assert _decide(tmp_path, ["docs/a.md"], base="", head="") == "full"
