"""41 的 editable 守卫判据（#609）—— 用 Python 的导入规则钉住，而不是靠面象。

`deploy/check-41-editable.sh` 跑在 41 上（需要 ssh），CI 里跑不了。但它的**判据**
可以在这里离线证：那个脚本的核心主张是

    site-packages 里的 `aiops_diagnostics/` 目录**允许存在**（hatch force-include
    把 `.env.example` / SOP / `faq_catalog.json` 装进了包目录），真正区分两种世界的
    是**它里面有没有 `__init__.py`** —— 有，它就成了常规包，赢过 editable 的 `.pth`。

这是 Python 的导入规则，不是本仓的约定，所以它可以在一个临时目录里被独立复现；
复现它比读一遍文档更有价值：判据一旦改错，这些断言会红，而真机上的误判不会。

两条路各测各的：导入规则用真解释器跑，**真机分支**（读 ssh 回来的四条观察再判）
用一个桩 `ssh` 驱动 —— 判据活在脚本里，不该为了可测而被抽成第二个实现。

先例：`tests/test_derive_zh_hant_tool.py` 用替身 converter 证 walker 的逻辑，
真机部分明确留给开发期检查。这里同一形状。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
GUARD = ROOT / "deploy" / "check-41-editable.sh"

_SP = "/opt/aiops-41/.venv/lib/python3.12/site-packages"
_SRC = "/opt/aiops-41/src"

#: What a healthy 41 answers to the guard's single ssh probe. Every case below is
#: this dict with exactly one field changed — so each assertion names one defect.
_HEALTHY = {
    "PKG_FILE": f"{_SRC}/aiops_diagnostics/__init__.py",
    "API_FILE": f"{_SRC}/aiops_diagnostics/gateway_api.py",
    "HAS_INIT": "no",
    "HAS_PTH": "yes",
    "PTH_TARGET": _SRC,
    "FAQ_DATA": f"{_SRC}/aiops_diagnostics/faq_catalog.json",
}


def _stub_ssh(tmp_path: Path, observations: dict[str, str]) -> Path:
    """A fake `ssh` that ignores its arguments and prints the canned probe output.

    The guard's only outside dependency is `ssh`; stubbing that one boundary is
    what lets the real judging code run in CI. A stub that echoed nothing would
    pass for the wrong reason, so it prints a **complete** probe block and each
    test perturbs exactly one line.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body = "".join(f"printf '%s\\n' '{key}={value}'\n" for key, value in observations.items())
    stub = bin_dir / "ssh"
    stub.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def _run_guard(tmp_path: Path, observations: dict[str, str]) -> subprocess.CompletedProcess:
    bin_dir = _stub_ssh(tmp_path, observations)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(GUARD)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def _winner(tmp_path: Path, *, site_packages_has_init: bool) -> str:
    """Build the two worlds and report which copy of the package Python loads.

    ``site-packages`` comes FIRST on sys.path (that is its real position
    relative to an editable ``.pth``, which appends the src dir after it), so
    "who wins" here is exactly "who wins on 41".
    """
    sp = tmp_path / "site-packages" / "aiops_diagnostics"
    src = tmp_path / "src" / "aiops_diagnostics"
    sp.mkdir(parents=True)
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('WHO = "src"\n', encoding="utf-8")
    if site_packages_has_init:
        # A regular package in site-packages: this is the broken world.
        (sp / "__init__.py").write_text('WHO = "site-packages"\n', encoding="utf-8")
    else:
        # Force-include landed static assets here, but no code: namespace portion.
        (sp / "faq_catalog.json").write_text("{}\n", encoding="utf-8")

    env = {
        "PYTHONPATH": f"{tmp_path / 'site-packages'}:{tmp_path / 'src'}",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import aiops_diagnostics as a; print(a.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


# --- the rule itself: an import rule, so run the real interpreter -----------------


def test_a_regular_package_in_site_packages_wins_over_the_editable_path(tmp_path: Path) -> None:
    """带 `__init__.py` ⇒ site-packages 赢 ⇒ src 同步静默失效。这就是要拦的世界。

    同时说明为什么守卫不能写成「site-packages 里有没有那个目录」：**这个目录在两种
    世界里都存在**，它对这个判断没有信息量。
    """
    winner = _winner(tmp_path, site_packages_has_init=True)
    assert winner == str(tmp_path / "site-packages" / "aiops_diagnostics" / "__init__.py")
    assert (tmp_path / "site-packages" / "aiops_diagnostics").is_dir()


def test_assets_only_directory_does_not_shadow_src(tmp_path: Path) -> None:
    """没有 `__init__.py` ⇒ 只是命名空间片段 ⇒ src 的常规包照样赢。

    这是 `uv sync` 之后**正常**的状态（2026-10-09 在 41 上实测：那个目录里 12 个
    文件全是静态资产，一个 `.py` 都没有）。守卫若在这里报警，就是把正常态当故障。
    """
    winner = _winner(tmp_path, site_packages_has_init=False)
    assert winner == str(tmp_path / "src" / "aiops_diagnostics" / "__init__.py")
    assert (tmp_path / "site-packages" / "aiops_diagnostics").is_dir(), "目录在，但不算数"


# --- the script ------------------------------------------------------------------


def test_the_guard_script_is_executable_and_parses() -> None:
    assert GUARD.is_file(), "deploy/check-41-editable.sh 缺失"
    assert GUARD.stat().st_mode & 0o111, "守卫脚本不可执行"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-n", str(GUARD)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="self-check 用 POSIX 路径拼 PYTHONPATH")
def test_the_guard_self_check_runs_without_a_host() -> None:
    """`--self-check` 不碰 41，只跑上面那条规则 —— 所以 CI 里也能跑。"""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(GUARD), "--self-check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"self-check 失败：\n{result.stdout}\n{result.stderr}"
    assert "self-check passed" in result.stdout


def test_the_guard_passes_on_the_healthy_observations(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, _HEALTHY)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "editable guard passed" in result.stdout


@pytest.mark.parametrize(
    ("field", "broken"),
    [
        ("PKG_FILE", f"{_SP}/aiops_diagnostics/__init__.py"),
        ("API_FILE", f"{_SP}/aiops_diagnostics/gateway_api.py"),
        ("HAS_INIT", "yes"),
        ("HAS_PTH", "no"),
        ("FAQ_DATA", f"{_SP}/aiops_diagnostics/faq_catalog.json"),
    ],
)
def test_each_broken_observation_fails_the_guard(tmp_path: Path, field: str, broken: str) -> None:
    """四条观察各自独立致命：任何一条坏了都必须被单独报出来并退非零。

    `HAS_INIT=yes` 那一条是这条守卫存在的理由（site-packages 里成了常规包）；
    其余三条是手册里原有的检查。少任何一条，守卫就少拦一种「CD 成功但没换代码」。
    """
    result = _run_guard(tmp_path, {**_HEALTHY, field: broken})
    assert result.returncode != 0, f"{field}={broken!r} 没有被判为失败：\n{result.stdout}"
    assert "editable guard FAILED" in result.stderr
    assert "回滚" in result.stderr, "失败时必须指向回滚步骤，而不是只报一个红"


def test_a_missing_dot_pth_is_reported_by_name(tmp_path: Path) -> None:
    """`.pth` 消失与「解析到别处」是两种故障，措辞要能分开 —— 否则值班的人只知道红。"""
    result = _run_guard(tmp_path, {**_HEALTHY, "HAS_PTH": "no", "PTH_TARGET": ""})
    assert "_editable_impl_aiops_diagnostics.pth" in (result.stdout + result.stderr)
