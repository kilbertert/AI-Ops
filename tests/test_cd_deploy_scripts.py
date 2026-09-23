"""CD 部署脚本的形状与分支测试。

为什么这些断言在 Python 里：仓库的确定性检查由 pytest 跑。把 shell 侧的分支测试
接进来，它才会在每次 CI 时执行 —— 否则一段只在真机钻演里跑过的回滚逻辑会慢慢腐掉
（本轮已经发生过：三条 bug 里有两条在钻演中从未触发）。
"""

from __future__ import annotations

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
