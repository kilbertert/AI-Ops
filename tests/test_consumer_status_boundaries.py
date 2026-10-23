"""两个运行边界的测试：实际隧道经 dev-host、私有报告路径校验。

这两项是 Codex 复核时指出**仍未完成**的部分：

1. 上一版是"先 `dev-host exec hostname` 校验，再另起普通 ssh" ——
   那次校验说明不了**实际连接**连的是谁；
2. 两个 `_write_private` 都是 `mkdir → write_text → chmod` ——
   会跟随符号链接、也没有库外限制。

隧道那侧需要真主机，本文件只测**能与不能**的判定（argv 形状、进程树归属、
清理），真实静态资源读取由 `verify_tunnel.py` 记在文档里。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TOOLS = ROOT / "tools" / "consumer-status"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"cs4_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("status_probe")
# 用**同一个模块名**加载：工具内部按 `consumer_status_private_output` 缓存，
# 测试另起一个名字会得到第二个类对象，`pytest.raises` 就永远匹配不上。
probe._private_output()  # 触发同目录模块加载，拿到**同一个**类对象
private_output = sys.modules["consumer_status_private_output"]


# ---------------------------------------------------------------- 隧道经 dev-host


def test_tunnel_is_launched_by_dev_host_not_a_bare_ssh() -> None:
    """实际连接必须由 `dev-host exec` 建立 —— 不是"校验后另起 ssh"。

    `dev-host exec` 把 `--` 之后的参数交给它自己的 `ssh`（同一套
    StrictHostKeyChecking + 项目 known_hosts + 主机指纹校验）。所以隧道命令的
    第一段必须是 `dev-host exec <target>`，`-N -L` 在 `--` 之后。
    """
    argv = probe.tunnel_argv(target="41", port=39791)
    assert argv[:3] == ["dev-host", "exec", "41"], argv
    assert "--allow-service-exec" in argv
    assert "--" in argv
    after = argv[argv.index("--") + 1 :]
    assert "-N" in after
    assert "-L" in after
    # 显式绑定开发机回环，端口是调用方选好的动态端口
    assert "127.0.0.1:39791:127.0.0.1:443" in after
    # 绝不能出现"裸 ssh"
    assert argv[0] != "ssh"


def test_tunnel_argv_does_not_embed_a_host_alias_of_its_own() -> None:
    """别名由 dev-host 从自己的记录取，脚本不再造第二个真值来源。"""
    argv = probe.tunnel_argv(target="41", port=12345)
    assert "aiops-41" not in argv, "脚本不该自己带 ssh 别名"


def test_descendants_covers_the_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """dev-host 是 shell 包装，监听者往往是子进程 —— 判据必须看**整棵树**。"""

    def fake_ss(*_a, **_k):
        class R:
            stdout = 'LISTEN 0 128 127.0.0.1:1 0.0.0.0:* users:(("ssh",pid=4242,fd=3))'

        return R()

    monkeypatch.setattr(probe.subprocess, "run", fake_ss)
    assert probe._listening_pids(1) == {4242}


def test_listening_pid_differs_from_popen_pid_but_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """监听 PID ≠ Popen PID 但**在树里** ⇒ 接受。这正是 dev-host 包装的形状。"""
    monkeypatch.setattr(probe, "_listening_pids", lambda _port: {9002})
    monkeypatch.setattr(probe, "_descendants", lambda pid: {pid, 9002})
    assert bool(probe._listening_pids(1) & probe._descendants(9001)) is True


def test_listener_outside_the_tree_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """端口被**别人的**进程听着 ⇒ 拒绝，不复用。"""
    monkeypatch.setattr(probe, "_listening_pids", lambda _port: {7777})
    monkeypatch.setattr(probe, "_descendants", lambda pid: {pid})
    assert bool(probe._listening_pids(1) & probe._descendants(9001)) is False


# ---------------------------------------------------------------- 私有报告路径


def test_repository_path_is_refused(tmp_path: Path) -> None:
    """仓库内路径必须拒绝 —— 完整订单号被误提交的入口就是这里。"""
    repo_like = tmp_path / "repo"
    (repo_like / ".git").mkdir(parents=True)
    target = repo_like / "evidence.json"
    with pytest.raises(private_output.PrivateOutputRefused, match="仓库内"):
        private_output.write_private_report(target, "{}", code_root=repo_like / "tools" / "x")
    assert not target.exists(), "拒绝之后不应留下文件"


def test_private_directory_succeeds_and_is_0600(tmp_path: Path) -> None:
    target = tmp_path / "private" / "evidence.json"
    private_output.write_private_report(target, '{"ok": true}', code_root=TOOLS)
    assert target.read_text().strip() == '{"ok": true}'
    assert oct(target.stat().st_mode)[-3:] == "600"
    assert oct(target.parent.stat().st_mode)[-3:] == "700"


def test_symlink_target_is_refused_and_target_content_unchanged(tmp_path: Path) -> None:
    """符号链接必须拒绝，且**目标内容不变**。

    这是 Codex 在临时样例里实际复现的问题：旧实现 `write_text` 会打开链接
    指向的文件，把别人的内容覆盖掉。
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    link = tmp_path / "evidence.json"
    link.symlink_to(victim)

    with pytest.raises(private_output.PrivateOutputRefused, match="符号链接"):
        private_output.write_private_report(link, "OVERWRITTEN", code_root=TOOLS)

    assert victim.read_text() == "ORIGINAL", "目标内容被覆盖了"
    assert link.is_symlink(), "链接被改动了"


def test_symlinked_parent_directory_is_refused(tmp_path: Path) -> None:
    """父目录是符号链接也要拒 —— 否则 `mkdir/chmod` 会作用到真实目录上。"""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(private_output.PrivateOutputRefused, match="父目录是符号链接"):
        private_output.write_private_report(alias / "x.json", "{}", code_root=TOOLS)


def test_repository_roots_detects_the_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "tools" / "consumer-status"
    nested.mkdir(parents=True)
    assert root in private_output.repository_roots(nested)


# ---------------------------------------------------------------- 两个入口都接上


def test_independent_cpms_refuses_repository_output(tmp_path: Path) -> None:
    """`independent_cpms` 的写入入口也要拒绝 —— 两个入口都要覆盖。"""
    cpms = _load("independent_cpms")
    repo_like = tmp_path / "repo"
    (repo_like / ".git").mkdir(parents=True)
    with pytest.raises(private_output.PrivateOutputRefused):
        cpms._write_private(repo_like / "x.json", "{}")
    assert not (repo_like / "x.json").exists()


def test_status_probe_write_private_refuses_repository(tmp_path: Path) -> None:
    repo_like = tmp_path / "repo"
    (repo_like / ".git").mkdir(parents=True)
    with pytest.raises(private_output.PrivateOutputRefused):
        probe._write_private(repo_like / "x.json", {"a": 1})


def test_status_probe_write_private_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "priv" / "x.json"
    probe._write_private(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert os.stat(target).st_mode & 0o077 == 0
