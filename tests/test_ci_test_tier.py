"""CI 默认全量，仅 Markdown 白名单可选文档档。

执行 workflow 原文，避免测试副本与实际门禁漂移；测试源码引用选择的文档集合
仍是启发式，不代表完整依赖分析。
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


def _decide_outputs(
    tmp_path: Path, changed: list[str], *, base: str = "aaa", head: str = "bbb"
) -> dict[str, str]:
    """跑 workflow 里那段脚本，返回它写出的全部 outputs。"""
    script = _tier_script()
    # 脚本用 `git diff --name-only` 取改动；用一个假 git 顶替，输出就是喂进去的清单。
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
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
            "PR_BASE": base,
            "PR_HEAD": head,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            parsed[key.strip()] = val.strip()
    return parsed


def _decide(tmp_path: Path, changed: list[str], *, base: str = "aaa", head: str = "bbb") -> str:
    """跑 workflow 里那段脚本，返回它写出的 tier（或 `""` 表示没跑判定）。"""
    return _decide_outputs(tmp_path, changed, base=base, head=head).get("tier", "")


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
        ["docs/check.py"],  # docs 中可执行内容仍走全量
        ["docs/fixture.json"],
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


def _selector_pattern() -> str:
    """`Run tests` 步骤里挑文档档测试集合用的那条正则，从 workflow 原文取出。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"targets=\$\(grep -rlE '([^']+)'", text)
    assert match, "ci.yml 里找不到文档档的选取正则（改名或改形状了？）"
    return match.group(1)


def test_the_docs_tier_selects_every_test_that_reads_docs() -> None:
    """读文档的方式不止 `docs/…` 一种写法。

    用 pathlib 拼路径的测试（`ROOT / "docs" / "agents" / "x.md"`）源码里**没有**
    `docs/` 这个字面量，只认字面量就会把它们留在档外：改那些文档时 CI 绿，
    而守着这些文档的测试一条都没跑。这里用「tests/ 里真的出现了哪一种写法」
    反推判据该认哪些写法，而不是再抄一份文件名清单。
    """
    pattern = _selector_pattern()
    reading = [
        path
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
        if re.search(r"""["']docs["']\s*/""", path.read_text(encoding="utf-8"))
    ]
    assert reading, "tests/ 里找不到用 pathlib 读 docs 的测试 —— 这条判据本身失效了"
    missed = [
        str(path.relative_to(REPO_ROOT))
        for path in reading
        if not re.search(pattern, path.read_text(encoding="utf-8"))
    ]
    assert not missed, f"文档档没选中这些读 docs 的测试，改文档时它们不会跑：{missed}"


def test_heavy_marker_routing_follows_changed_paths(tmp_path: Path) -> None:
    """只有命中对应域或基础配置的改动才在 PR 全量档激活 docker / browser / deploy_tooling。"""
    gateway_only = _decide_outputs(tmp_path / "gw", ["src/aiops_diagnostics/gateway/gateway_api.py"])
    assert gateway_only == {
        "tier": "full",
        "run_docker": "false",
        "run_browser": "false",
        "run_deploy_tooling": "false",
    }

    dify_change = _decide_outputs(tmp_path / "dify", ["src/aiops_diagnostics/dify/dify_trial.py"])
    assert dify_change == {
        "tier": "full",
        "run_docker": "true",
        "run_browser": "false",
        "run_deploy_tooling": "false",
    }

    consumer_change = _decide_outputs(
        tmp_path / "consumer", ["tools/verification/consumer-status/replay_drive.py"]
    )
    assert consumer_change == {
        "tier": "full",
        "run_docker": "false",
        "run_browser": "true",
        "run_deploy_tooling": "true",
    }

    tooling_change = _decide_outputs(tmp_path / "tooling", ["deploy/m2-live-03/checks.sh"])
    assert tooling_change == {
        "tier": "full",
        "run_docker": "false",
        "run_browser": "false",
        "run_deploy_tooling": "true",
    }

    infra_change = _decide_outputs(tmp_path / "ci", [".github/workflows/ci.yml"])
    assert infra_change == {
        "tier": "full",
        "run_docker": "true",
        "run_browser": "true",
        "run_deploy_tooling": "true",
    }

    docs_only = _decide_outputs(
        tmp_path / "docs",
        ["docs/adr/0355-tier-pytest-markers-and-ci-heavy-paths.md"],
    )
    assert docs_only == {
        "tier": "docs",
        "run_docker": "false",
        "run_browser": "false",
        "run_deploy_tooling": "false",
    }

    missing_base = _decide_outputs(tmp_path / "nobase", ["docs/a.md"], base="", head="")
    assert missing_base == {
        "tier": "full",
        "run_docker": "true",
        "run_browser": "true",
        "run_deploy_tooling": "true",
    }
