"""CD 部署脚本的形状与分支测试。

为什么这些断言在 Python 里：仓库的确定性检查由 pytest 跑。把 shell 侧的分支测试
接进来，它才会在每次 CI 时执行 —— 否则一段只在真机钻演里跑过的回滚逻辑会慢慢腐掉
（本轮已经发生过：三条 bug 里有两条在钻演中从未触发）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"


def _run(script: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_scripts_are_present_and_executable() -> None:
    for name in ("deploy-41.sh", "classify-remote-result.sh", "test-rollback-classifier.sh"):
        path = DEPLOY_DIR / name
        assert path.is_file(), f"{name} 缺失"
        assert path.stat().st_mode & 0o111, f"{name} 不可执行"


def test_deploy_scripts_parse() -> None:
    for script in sorted(DEPLOY_DIR.glob("*.sh")):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script.name} 语法错误：{result.stderr}"


def test_remote_block_renders_and_parses() -> None:
    """远端块是**生成**出来的（heredoc 展开），所以静态 `bash -n` 看不到它。

    这一条把 dry-run 的输出取出来单独做语法检查 —— 本轮就是这样抓到一个只在实际
    渲染后才出现的错误：heredoc 里写的函数用 `$1` 而没有转义，bash 在**本机**展开它，
    于是 `set -u` 下报 unbound variable。静态检查看不到，因为它那时还只是一段字符串。
    """
    repo = DEPLOY_DIR.parent
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(DEPLOY_DIR / "deploy-41.sh"), "--commit", "HEAD", "--dry-run"],
        capture_output=True,
        cwd=repo,
        check=False,
        env={**os.environ, "PATH": f"/home/claude/miniconda3/bin:{os.environ.get('PATH', '')}"},
    )
    # 部署脚本里有中文；`cut -c` 之类会从多字节字符中间截断。用 errors="replace"
    # 避免因为输出层的一个坏字节让整条检查失败 —— 我们要查的是结构，不是编码。
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, f"dry-run 失败：\n{stdout}\n{stderr}"
    # 取出 dev-host exec 与 dry-run 结束之间的那段（就是将要发给主机的脚本）
    lines = stdout.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("dev-host exec"))
    end = next(i for i, line in enumerate(lines) if "dry run 结束" in line)
    block = "\n".join(lines[start + 1 : end])
    assert "mutate_rc" in block, "远端块缺少变更阶段的状态累积"
    assert block.count("health_version()") == 1, "health_version 应只定义一次"
    checked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-n", "/dev/stdin"], input=block, capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0, f"生成的远端块语法错误：{checked.stderr}"


def test_set_e_guard_requires_escaped_positional() -> None:
    """heredoc 里定义的函数若用 `$1` 而不转义，bash 会在本机展开它。

    本轮踩到过：`step_failed() { echo "mutate-failed=$1"; ... }` 写在未加引号的 heredoc
    里，`set -u` 下 dry-run 直接 unbound。静态 `bash -n` 看不到 —— 那时它只是字符串。
    """
    source = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    start = source.index("read -r -d '' REMOTE_CMD")
    end = source.index("\nEOF", start)
    block = source[start:end]
    for line in block.split("\n"):
        stripped = line.strip()
        # 只看字符串字面量里的 $N（函数体里的裸 $1 在 heredoc 内会被本机展开）
        if "echo" in stripped and re.search(r'"[^"]*\$[1-9]', stripped) and "\\$" not in stripped:
            pytest.fail(f"heredoc 内的位置参数未转义，会在本机展开：{stripped}")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未安装")
def test_deploy_scripts_pass_shellcheck() -> None:
    scripts = [str(p) for p in sorted(DEPLOY_DIR.glob("*.sh"))]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["shellcheck", *scripts], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"shellcheck 失败：\n{result.stdout}\n{result.stderr}"


def test_rollback_classifier_branches() -> None:
    """回滚分类器的 7 个分支必须全部通过。

    单独跑一次这个测试，是为了让「回滚判定」的每条分支都在 CI 里有名字 ——
    其中 restart-非零 与 标记走 stderr 两条，真机钻演从未触发过。
    """
    result = _run(DEPLOY_DIR / "test-rollback-classifier.sh")
    assert result.returncode == 0, f"回滚分类器分支测试失败：\n{result.stdout}\n{result.stderr}"
    assert "0 失败" in result.stdout


def test_classifier_markers_match_deploy_emitters() -> None:
    """主机发出的标记集合，必须覆盖分类器读取的标记集合。

    两边各写一份字符串字面量 —— 正是这个仓一直在打的「两处实现会漂移」。这里用一条
    断言把它钉住：分类器读什么，主机就必须发什么。
    """
    deploy = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    classify = (DEPLOY_DIR / "classify-remote-result.sh").read_text(encoding="utf-8")
    for marker in ("rollback=restored", "rollback=also-failed", "rollback-health=", "rollback-active="):
        assert marker in classify, f"分类器未读取 {marker}"
        assert marker in deploy, f"主机未发出分类器读取的 {marker}"
