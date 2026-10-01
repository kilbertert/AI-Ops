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
CD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cd.yml"


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


@pytest.mark.skipif(
    shutil.which("dev-host") is None,
    reason="dry-run 需要 dev-host（只在开发机上）—— CI 是 GitHub-hosted，没有它",
)
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


def test_routing_window_refuses_values_that_would_reach_the_remote_shell() -> None:
    """取数脚本的四个环境变量都会被嵌进远端命令，逐个按形状拒。

    它平时只跑默认值，所以这些分支靠人跑到的那一天，就是它出错的那一天。
    换行单独测：`grep -E` 是逐行匹配的，「合法首行 + 换行 + 命令」会在正则那关通过，
    而远端 shell 会执行第二行。
    """
    script = DEPLOY_DIR / "routing-window.sh"
    window = "2026-10-01T00:00:00+00:00"
    cases = [
        ("AIOPS_41_SERVICE", "aiops-gateway-41; id"),
        ("AIOPS_41_HOST", "host && id"),
        ("AIOPS_41_DB", "/var/lib/x.db; id"),
        ("AIOPS_41_PY", "/usr/bin/python\nid"),
        ("AIOPS_41_PY", "/usr/bin/python\r\nid"),
    ]
    for name, value in cases:
        env = {**os.environ, name: value}
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["bash", str(script), window], capture_output=True, text=True, check=False, env=env
        )
        assert result.returncode == 2, f"{name}={value!r} 未被拒绝：{result.stdout}"


def test_routing_window_refuses_an_impossible_window_start() -> None:
    """形状对不等于时刻存在：不在日历上的值会被拒，而不是静默算窄窗口。

    那个值会按**文本**与 created_at 比较，所以一个存不存在的月份不会报错，
    只会让窗口悄悄变一个宽度。
    """
    script = DEPLOY_DIR / "routing-window.sh"
    for value in ("2026-13-01T02:00:00+00:00", "2026-10-01T25:00:00+00:00", "不是时间"):
        result = _run(script, value)
        assert result.returncode == 2, f"{value!r} 未被拒绝：{result.stdout}"
    for value in ("2026-10-01T02:00:00+08:00", "2026-10-01", "2026-10-01T02:00:00Z"):
        result = _run(script, value)
        assert result.returncode == 2, f"{value!r} 未被拒绝（只收 UTC 的 +00:00 形状）"


def test_routing_window_says_what_it_observed(tmp_path: Path) -> None:
    """无指标行时说「无法据此判断网关是否处理过请求」，不说「没处理过请求」。

    `/health` 与媒体路由不写指标行，所以空窗口与「有流量但不进这张表」不可分。
    这条断言的是一条措辞纪律：只陈述观测到的事实。
    """
    source = (DEPLOY_DIR / "routing-window.sh").read_text(encoding="utf-8")
    assert "据此无法判断网关是否处理过请求" in source
    assert "网关没有处理过请求" not in source


def _cd_push_paths(workflow: Path = CD_WORKFLOW) -> list[str]:
    """读 cd.yml 的 `push.paths`。

    **不引 YAML 库**：加一个 dev 依赖会改 uv.lock，而 deploy-41.sh 的依赖漂移门
    拿 uv.lock 的 sha256 与 41 上的比 —— 于是「加一个测试依赖」会**拦住整条 CD**，
    直到有人按 docs/agents/env-41-dependency-update.md 的人工流程更新 41 的环境。
    一个测试助手不值这个代价，所以这里按缩进切。

    切得出来的前提是 `on.push.paths` 是**列表**形状的块序列（每项 `      - "..."`）。
    它变了这条会抛错而不是静默返回空 —— 空列表会让下面三条断言全过，那正是这里
    最危险的失败模式。**这个助手自己可失败，由
    test_cd_paths_guard_reads_the_real_block 用两份改坏的副本驱动。**
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line == "    paths:")
        end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("      "))
    except StopIteration:
        raise AssertionError("cd.yml 里找不到 push.paths 块（形状变了，先看这段的注释）") from None
    paths = [
        match.group(1) for line in lines[start + 1 : end] if (match := re.match(r'^      - "([^"]+)"$', line))
    ]
    # 块里每一项都该是路径。注释行与空行不算，但**一项都没解析出来**说明形状变了。
    assert paths, f"push.paths 块在 {workflow.name}:{start + 1} 解析为空 —— 缩进或引号形状变了"
    return paths


def _deploy41_dependencies() -> set[str]:
    """deploy-41.sh **执行**的 deploy/ 文件。

    只认调用形状（`"$REPO_ROOT/deploy/<name>"`），不认任意出现 —— 注释里提到的
    文件名不是依赖，把它们算进来会让这条断言因为一句解释文字而变红。
    """
    source = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    return set(re.findall(r'"\$REPO_ROOT/deploy/([A-Za-z0-9._-]+)"', source))


def test_cd_paths_covers_everything_deploy41_executes() -> None:
    """`paths` 必须命中 deploy-41.sh 会执行的每个 deploy/ 文件。

    `paths` 不命中时**不会有任何信号**：workflow 根本不创建 run，所以「改了却没部署」
    与「改的东西不用部署」在 GitHub 上长得一模一样。这正是参考资料那段注释记的坑，
    #520 收窄 `deploy/**` 时把同一个坑重新挖到两个具体文件名上，所以在这里钉住。

    `deploy-41.sh` 自己写在断言里而不是从脚本里推：它是**被执行的**那一个，不是被引用的
    那一个，所以 `_deploy41_dependencies` 的调用形状正则天然扫不到它（自己不会写自己）。
    """
    paths = _cd_push_paths()
    assert "deploy/deploy-41.sh" in paths, "workflow 执行的 deploy-41.sh 本身必须在触发集合里"
    dependencies = _deploy41_dependencies()
    assert dependencies, "deploy-41.sh 里解析不出任何被调用的 deploy/ 文件 —— 调用形状变了"
    for dep in dependencies:
        assert f"deploy/{dep}" in paths, f"deploy-41.sh 会执行 {dep}，但 cd.yml 的 paths 不触发它"


def test_cd_paths_does_not_trigger_on_read_only_scripts() -> None:
    """`deploy/` 下的只读取数脚本不得触发部署（#520 的判据）。

    这条是 #520 的回归闸：`deploy/**` 恢复回来，它就红。理由是实测的代价 ——
    #513/#514 只改了 company-gitlab-api.sh（一个只读脚本，网关不 import 它），
    却各拉起一次生产重启，并把 #405 的观察窗重新计时（窗口起点 = 网关进程启动时刻）。
    """
    paths = _cd_push_paths()
    assert "deploy/**" not in paths, "deploy/** 会让只读脚本也触发生产重启（#520）"
    for name in ("company-gitlab-api.sh", "routing-window.sh", "d4-cutover.py", "record-manual-deploy.sh"):
        assert f"deploy/{name}" not in paths, f"{name} 与部署无关，不该触发生产重启"


def test_cd_paths_still_covers_reference_files() -> None:
    """`paths` 必须覆盖 deploy-41.sh 的 REFERENCE_FILES（CD-41-14 的机器化）。

    改了 SOP 却不部署时，生产会**静默**继续用旧规则 —— 没有报错。所以这条不是
    「顺手加的」，是原本就存在、只是从未被机器验证过的约束（qa-plan.md 的 CD-41-14
    写的是「已脚本化比对」，实际没有脚本；本 PR 把它补上）。
    """
    deploy = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    match = re.search(r'^REFERENCE_FILES="([^"]+)"', deploy, re.MULTILINE)
    assert match, "deploy-41.sh 里找不到 REFERENCE_FILES"
    paths = _cd_push_paths()
    for ref in match.group(1).split():
        assert ref in paths, f"参考资料 {ref} 改了不触发部署，生产会静默用旧规则"


def test_cd_paths_guard_reads_the_real_block(tmp_path: Path) -> None:
    """上面这个助手自己可失败：改坏的两份副本必须让它抛错，而不是静默返回空。

    `_cd_push_paths` 返回空列表时，三条断言会**全部通过** —— 一个恒真的守卫比没有
    守卫更糟，因为它看起来像在守着。所以这里驱动它自己的失败模式：块被注释掉、
    引号被去掉。两份都取自**真实的 cd.yml 文本**再改，不是手写的假文件。
    """
    real = CD_WORKFLOW.read_text(encoding="utf-8")
    assert "    paths:" in real, "真实 cd.yml 里没有锚点行 —— 助手已经失效了"

    commented = tmp_path / "commented.yml"
    commented.write_text(re.sub(r"^      - ", "      # - ", real, flags=re.MULTILINE), encoding="utf-8")
    with pytest.raises(AssertionError, match="解析为空"):
        _cd_push_paths(commented)

    unquoted = tmp_path / "unquoted.yml"
    unquoted.write_text(
        re.sub(r'^      - "([^"]+)"$', r"      - \1", real, flags=re.MULTILINE), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="解析为空"):
        _cd_push_paths(unquoted)

    missing = tmp_path / "missing.yml"
    missing.write_text(real.replace("    paths:", "    pathsXYZ:"), encoding="utf-8")
    with pytest.raises(AssertionError, match="找不到"):
        _cd_push_paths(missing)
