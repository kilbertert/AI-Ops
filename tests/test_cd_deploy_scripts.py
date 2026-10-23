"""CD 部署脚本的形状与分支测试。

为什么这些断言在 Python 里：仓库的确定性检查由 pytest 跑。把 shell 侧的分支测试
接进来，它才会在每次 CI 时执行 —— 否则一段只在真机钻演里跑过的回滚逻辑会慢慢腐掉
（本轮已经发生过：三条 bug 里有两条在钻演中从未触发）。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"
CD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cd.yml"

#: A directory holding a stand-in **named `dev-host`**, so it can shadow the real
#: tool on PATH. The name is load-bearing: `deploy-41.sh` resolves it with
#: `command -v dev-host`, so a file called `dev-host-stub.sh` would never be found —
#: measured, and it made the test silently keep talking to the real host.
DEV_HOST_STUB_DIR = REPO_ROOT / "tests" / "fixtures" / "dev-host-stub"


def _run_bounded(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv`` and, on timeout, take its **whole process tree** down.

    ``subprocess.run(timeout=…)`` only signals the child it started. That is the
    wrong shape here: the child is `bash`, which starts `dev-host`, which starts
    `ssh`. Killing bash leaves those running, and — because SIGKILL bypasses
    bash's EXIT trap — it also skips the `mktemp -d` staging removal that trap
    does. A timeout would then leak both a process and a staging directory while
    reporting a clean failure. Both halves measured on this repository,
    2026-10-18; caught by review on the first version of this change.

    So: put the child in its own process group and signal the GROUP. `start_new_session`
    is the Python spelling of `setsid`, which makes the child a session and group
    leader, so every descendant inherits the group unless it deliberately leaves.
    SIGTERM first gives bash the chance to run its trap; the SIGKILL after the wait
    covers a child that ignores it.
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


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
    not (DEV_HOST_STUB_DIR / "dev-host").is_file(),
    reason="dev-host 替身缺失（tests/fixtures/dev-host-stub.sh）",
)
def test_remote_block_renders_and_parses() -> None:
    """远端块是**生成**出来的（heredoc 展开），所以静态 `bash -n` 看不到它。

    这一条把 dry-run 的输出取出来单独做语法检查 —— 本轮就是这样抓到一个只在实际
    渲染后才出现的错误：heredoc 里写的函数用 `$1` 而没有转义，bash 在**本机**展开它，
    于是 `set -u` 下报 unbound variable。静态检查看不到，因为它那时还只是一段字符串。

    **跑在 `dev-host` 替身上，不是真主机上。** 脚本在渲染远端块之前要做六次
    `dev-host` 往返（健康探针、参考资料存在性、两份依赖哈希）。对着真 41 每次约
    4.4 秒（2026-10-22 实测），合计 28 秒 —— 而这一条要验的是「块渲染出来能不能解析」，
    不需要知道生产此刻在跑哪个版本。那六次读是**提问的成本**，不是被测物。

    替身保真度是可核对的，不是自称的：经它渲染出的块与对着真主机渲染出的块
    **逐字节相同**（152 行，`cmp` 通过；复核方式见 `tests/fixtures/dev-host-stub.sh`）。
    这也正是本仓 ADR 0030 与 1.7.27 下发的那条判据 —— 替身必须比现实更苛刻或同样苛刻，
    否则测试绿得没有意义。

    于是这条在 CI 上也跑得起来了：原先它 `skipif` 依赖本机有 `dev-host`，
    在 GitHub-hosted runner 上永远跳过 —— 一个**只在开发机上执行**的形状检查。

    ⚠️ `timeout` 是必需的，不是保险。这条是本仓**唯一**把外部工具（`bash deploy-41.sh`）
    拉起来却没有时限的 subprocess 调用 —— AST 扫描 `tests/`：46 处 subprocess，
    33 处无 timeout，其中只有这一条拉起外部工具，其余是 `git`/本机 python 之类本机快命令。
    它一旦挂住，CI 只能等 job 的 `timeout-minutes: 20` 才失败 —— 20 分钟里没有任何输出
    告诉人卡在哪。

    ⚠️ 用的是 `_run_bounded` 而不是 `subprocess.run(..., timeout=…)`：后者只杀它
    直接起的那个子进程；且 SIGKILL 会跳过 bash 的 EXIT trap，于是 `mktemp -d` 的暂存
    目录也留在盘上。拒绝要拒绝得干净。
    """
    repo = DEPLOY_DIR.parent
    # The stub shadows the real `dev-host` by sitting earlier on PATH. Everything
    # else on PATH is untouched, so the script still finds git/python3/curl.
    stub_dir = DEV_HOST_STUB_DIR
    result = _run_bounded(  # noqa: S603 - fixed argv, no shell
        ["bash", str(DEPLOY_DIR / "deploy-41.sh"), "--commit", "HEAD", "--dry-run"],
        cwd=repo,
        timeout=300,
        env={
            **os.environ,
            "PATH": f"{stub_dir}:/home/claude/miniconda3/bin:{os.environ.get('PATH', '')}",
            # The script also reads a host registry before it renders anything, and
            # a GitHub-hosted runner has none — that was the second reason this test
            # never ran in CI. The fixture carries the shape of the real `[hosts.aiops-41]`
            # entry (see its header); only the fields the script parses are present.
            "DEV_HOST_REGISTRY": str(stub_dir / "hosts.toml"),
        },
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


# --------------------------------------------------------------------------
# CD registry 派生：类型必须原样往返
# --------------------------------------------------------------------------


def _registry_renderer() -> str:
    """取出 deploy-41.sh 里那段派生脚本的**真实源码**。

    不复制一份 Python 到测试里 —— 那样测的是副本，脚本改了测试不会红。
    """
    script = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    match = re.search(
        r"python3 - \"\$CANONICAL_REGISTRY\" \"\$CD_REGISTRY\" \"\$CD_ALIAS\" <<'PY'\n(.*?)\nPY\n",
        script,
        re.DOTALL,
    )
    assert match is not None, "deploy-41.sh 里找不到派生 registry 的那段 heredoc"
    return match.group(1)


def _render_registry(tmp_path: Path, canonical_text: str) -> dict[str, object]:
    """把一段主清单喂给 `deploy-41.sh` 里**真实**的派生脚本，读回派生视图。"""
    source = tmp_path / "canonical.toml"
    source.write_text(canonical_text, encoding="utf-8")
    derived = tmp_path / "derived.toml"

    namespace: dict[str, object] = {"__name__": "registry_renderer"}
    saved = sys.argv
    sys.argv = ["renderer", str(source), str(derived), "aiops-41-cd"]
    try:
        exec(compile(_registry_renderer(), "<deploy-41.sh>", "exec"), namespace)  # noqa: S102
    finally:
        sys.argv = saved

    import tomllib

    with derived.open("rb") as handle:
        return tomllib.load(handle)["hosts"]["aiops-41"]


def test_registry_view_round_trips_non_string_types(tmp_path: Path) -> None:
    """派生视图必须**类型无损**：数组还是数组，布尔还是布尔。

    这里曾经把每个值都写成 `"%s"`，于是清单里的 `aka = ["41"]`（数组）被写成
    字符串 `"['41']"`，读回来是 str —— 派生视图与主清单永远不相等，dry-run 与
    真实部署都会停在一致性检查。它不是 M3 的问题，但它阻断每一次部署。
    """
    view = _render_registry(
        tmp_path,
        "[hosts.aiops-41]\n"
        'aka = ["41", "gateway-41"]\n'
        'ssh_alias = "manual-41"\n'
        'role = "service-host"\n'
        "write_gate = true\n"
        "retries = 3\n"
        "timeout = 2.5\n",
    )
    assert view["aka"] == ["41", "gateway-41"], "数组被写成了字符串"
    assert isinstance(view["aka"], list)
    assert view["write_gate"] is True
    assert view["retries"] == 3
    assert view["timeout"] == 2.5
    assert view["ssh_alias"] == "aiops-41-cd"


def test_registry_view_escapes_control_characters(tmp_path: Path) -> None:
    """控制字符必须写成转义序列，不能写成物理字符。

    主清单里的 `"first\\nsecond"` 是**合法 TOML**：解析出来是一个带换行的字符串。
    旧渲染把这个换行原样写进单行字符串 —— TOML 不允许，派生文件根本解析不了，
    于是部署与回滚都停在那条一致性检查上。
    """
    view = _render_registry(
        tmp_path,
        "[hosts.aiops-41]\n"
        'aka = ["first\\nsecond", "tab\\there", "back\\bspace", "cr\\rhere"]\n'
        'note = "quote \\" and backslash \\\\"\n',
    )
    assert view["aka"] == ["first\nsecond", "tab\there", "back\bspace", "cr\rhere"]
    assert view["note"] == 'quote " and backslash \\'


def test_registry_view_quotes_dotted_inline_table_keys(tmp_path: Path) -> None:
    """行内表的键要加引号：`"contact.name"` 是**一个**键，不是嵌套表。"""
    view = _render_registry(
        tmp_path,
        '[hosts.aiops-41]\nmeta = { "contact.name" = "ops", "with space" = "x" }\n',
    )
    assert view["meta"] == {"contact.name": "ops", "with space": "x"}


def _dependency_signature(text: str) -> str:
    """`pyproject.toml` 里**决定 restart 能否 import 成功**的那部分，规范化后成串。

    用真解析器而不是正则：注释、空行、键序、`addopts` 都不该影响它 —— 那正是本条
    规则要区分的东西。
    """
    import json
    import tomllib

    data = tomllib.loads(text)
    project = data.get("project", {})
    return json.dumps(
        {
            "dependencies": project.get("dependencies", []),
            "optional": project.get("optional-dependencies", {}),
            "groups": data.get("dependency-groups", {}),
            "requires_python": project.get("requires-python"),
        },
        sort_keys=True,
    )


def test_dependency_drift_ignores_comment_only_changes() -> None:
    """注释与工具配置的改动不是依赖漂移。

    实测 2026-10-23：一次只改注释与 `addopts` 的提交让 `pyproject.toml` 的 sha256
    变了，`deploy-41.sh` 因此停止部署 —— 而 pyproject 与 uv.lock 里每一条依赖都
    逐字节相同。判据问的是「restart 会不会缺包」，注释回答不了这个问题；按字节比
    下去，**每一次改注释都会把 CD 卡到有人去生产机上手工跑依赖更新为止**。

    这条测试钉住两件事：注释不影响签名，而**真的改依赖仍然影响**。只钉前一半会让
    「签名恒为空」也通过。
    """
    base = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    commented = base + "\n# a new comment that changes the bytes but not the deps\n"
    assert commented != base
    assert _dependency_signature(commented) == _dependency_signature(base)

    # 对照组：真的加一条运行期依赖必须被看出来，否则上面那半条只是「函数恒返回同值」。
    changed = base.replace('  "redis>=5.2,<7",', '  "redis>=5.3,<7",', 1)
    assert changed != base, "fixture drifted: pyproject no longer contains the redis pin this test edits"
    assert _dependency_signature(changed) != _dependency_signature(base)


def test_deploy_compares_pyproject_by_declarations_not_bytes() -> None:
    """`deploy-41.sh` 的依赖漂移门对 pyproject 用语义比较，对 uv.lock 仍用字节。

    uv.lock 是机读产物：差一个字节就是不同的解析结果，所以那条比较不该改。
    pyproject 是人写的：注释与工具配置住在这里，用字节比会把它们误判成依赖变化。
    """
    source = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    assert 'if [ "$spec" = "pyproject.toml" ]; then' in source
    assert "tomllib" in source, "语义比较没被写进脚本"
    # uv.lock 仍在同一循环里按 sha256 比，不能被顺手一起放宽。
    assert 'for spec in "pyproject.toml" "uv.lock"; do' in source


def test_deploy_compares_uv_lock_by_resolved_packages() -> None:
    """`uv.lock` 里的本项目版本号变更不是依赖漂移。

    `uv.lock` 的记录含 `name = "aiops-diagnostics"` + `version`，所以发版会改写它。
    按字节比会把「发布 0.2.0」判成依赖漂移，**拦住这次发布的部署**——实测 2026-10-23：
    0.1.0 → 0.2.0 时 `uv.lock` 唯一的改动就是那行 version。

    判据仍是严格的：除本项目外，每个包的 name+version 都必须相同。
    """
    import json
    import tomllib

    def packages(text: str) -> str:
        data = tomllib.loads(text)
        return json.dumps(
            sorted(
                [p.get("name"), p.get("version")]
                for p in data.get("package", [])
                if p.get("name") and p.get("name") != "aiops-diagnostics"
            ),
            sort_keys=True,
        )

    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    bumped = lock.replace(
        'name = "aiops-diagnostics"\nversion = "', 'name = "aiops-diagnostics"\nversion = "9.9.9"  # was 0'
    )
    assert bumped != lock
    assert packages(bumped) == packages(lock), "本项目版本号变更不应改变包集合"

    # 对照组：真依赖的版本变了必须被看出来，否则上面那半条只是「函数恒返回同值」。
    assert '"redis"' in lock or "name = 'redis'" in lock or 'name = "redis"' in lock
    changed = lock.replace('name = "redis"\nversion = "', 'name = "redis"\nversion = "9.9.9"  # was 0')
    assert changed != lock, "fixture drifted: uv.lock no longer pins redis in the expected shape"
    assert packages(changed) != packages(lock)


def test_deploy_compares_uv_lock_semantically() -> None:
    """`deploy-41.sh` 里 uv.lock 必须**真的**走语义比较，而不是只在注释里说。

    第一版这条只查 `"lock_packages()" in source` —— 把调用处改成 `if false` 仍然
    全绿，因为定义还在。**查字符串存在是假绿**：它看不见「这段代码会不会被执行」。
    所以这里跑脚本里**实际的** `lock_packages`，用两份锁喂它。
    """
    import json
    import re
    import subprocess

    source = (DEPLOY_DIR / "deploy-41.sh").read_text(encoding="utf-8")
    match = re.search(r"^lock_packages\(\) \{\n(.*?)^\}$", source, re.M | re.S)
    assert match, "deploy-41.sh 里找不到 lock_packages 函数"

    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    own = 'name = "aiops-diagnostics"\nversion = "'
    dep = 'name = "redis"\nversion = "'
    bumped = lock.replace(own, own + "9.9.9")
    changed = lock.replace(dep, dep + "9.9.9")

    def run(text: str) -> str:
        script = "\n".join(
            line[2:] if line.startswith("  ") else line for line in match.group(1).splitlines()
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["bash", "-c", script], input=text, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    same = run(lock)
    assert same, "lock_packages 对真实 uv.lock 没有输出"
    assert json.loads(same) == json.loads(run(bumped)), "本项目版本号变更被判成了包集合变化"
    assert json.loads(same) != json.loads(run(changed)), "真依赖版本变更没有被看出来"
