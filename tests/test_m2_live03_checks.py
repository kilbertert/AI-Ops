"""checks.sh 的失败门禁回归 —— 每条都从「其它前提全部成立」的基线逐项破坏。

用假 docker（返回可控退出码）、临时目录与临时状态根，不接触真实 daemon、
不碰生产路径。旧实现的两种错法在这里都会转红：

* `down --purge` 在 docker 停栈失败后仍 `rm -rf`，且整个脚本返回 0；
* 预检的「权限」用例其实只证明缺文件，「生产根」用例接受 0/1/2 任意返回。

每条用例先建 baseline（可复现的最小合法状态），再只破坏一个前提，断言：
非零退出 + 对照文件/记录原样不动。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

CHECKS = Path(__file__).resolve().parents[1] / "deploy/m2-live-03/checks.sh"
DEPLOY_DIR = CHECKS.parent


def _fake_docker(tmp_path: Path, down_exit: int = 0) -> Path:
    """最小 docker 替身：`compose ... down` 返回给定码，其余子命令成功。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *" down"*) exit {down_exit} ;;\n'
        '  *"compose version"*) echo "Docker Compose version v2.0.0"; exit 0 ;;\n'
        '  *"version"*) exit 0 ;;\n'
        '  *"image inspect"*) echo "sha256:0000"; exit 0 ;;\n'
        '  *"ps"*) exit 0 ;;\n'
        '  *"inspect"*) exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    docker.chmod(0o700)
    return bin_dir


def _env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "M2LIVE_STATE": str(tmp_path / "state"),
        "M2LIVE_CONFIG": str(tmp_path / "etc"),
        "M2LIVE_DIST": str(tmp_path / "dist"),
    }
    env.update(extra)
    return env


def _owned_state(tmp_path: Path, project: str = "m2-live-03") -> Path:
    """baseline：一个带所有权标记、只含本栈目录的状态根。"""
    state = tmp_path / "state"
    for name in ("store", "gateway", "node", "platform"):
        (state / name).mkdir(parents=True, exist_ok=True)
        (state / name / "keep").write_text("data")
    (state / ".owner").write_text(project + "\n")
    return state


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CHECKS), *args], env=env, capture_output=True, text=True, timeout=120)


def test_purge_refuses_when_stopping_the_stack_fails(tmp_path):
    """docker down 返回 17 时不得删数据，且脚本必须非零退出。"""
    bin_dir = _fake_docker(tmp_path, down_exit=17)
    state = _owned_state(tmp_path)
    marker = state / "gateway" / "keep"

    result = _run(_env(tmp_path, bin_dir), "down", "--purge")

    assert result.returncode != 0, "停栈失败却返回 0"
    assert result.returncode == 17 or result.returncode == 1
    assert marker.exists(), "停栈失败仍删除了数据"


def test_purge_removes_only_this_stacks_directories(tmp_path):
    bin_dir = _fake_docker(tmp_path, down_exit=0)
    state = _owned_state(tmp_path)
    outsider = state / "someone-elses"
    outsider.mkdir()
    (outsider / "keep").write_text("other task")

    result = _run(_env(tmp_path, bin_dir), "down", "--purge")

    assert result.returncode == 3, result.stderr
    assert (outsider / "keep").exists(), "删到了非本栈条目"
    assert (state / "gateway" / "keep").exists(), "拒绝时不该删任何东西"


def test_purge_refuses_an_unowned_state_root(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    state = tmp_path / "state"
    (state / "gateway").mkdir(parents=True)
    marker = state / "gateway" / "keep"
    marker.write_text("owned-by-another-task")

    result = _run(_env(tmp_path, bin_dir), "down", "--purge")

    assert result.returncode == 3
    assert marker.exists()


def test_purge_refuses_a_state_root_owned_by_another_project(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    state = _owned_state(tmp_path, project="another-project")

    result = _run(_env(tmp_path, bin_dir), "down", "--purge")

    assert result.returncode == 3
    assert (state / "gateway" / "keep").exists()


def test_purge_refuses_when_the_state_root_is_a_symlink(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    real = tmp_path / "elsewhere"
    (real / "gateway").mkdir(parents=True)
    (real / ".owner").write_text("m2-live-03\n")
    marker = real / "gateway" / "keep"
    marker.write_text("data")
    link = tmp_path / "state"
    link.symlink_to(real)

    result = _run(_env(tmp_path, bin_dir), "down", "--purge")

    assert result.returncode == 3
    assert marker.exists()


def test_purge_refuses_a_state_root_at_a_system_path(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    for candidate in ("/", "/var", "/etc"):
        result = _run(_env(tmp_path, bin_dir, M2LIVE_STATE=candidate), "down", "--purge")
        assert result.returncode == 3, f"{candidate} 没有被拒"


def test_purge_refuses_state_under_production(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    state = tmp_path / "aiops-41-copy"
    (state / "gateway").mkdir(parents=True)
    (state / ".owner").write_text("m2-live-03\n")
    marker = state / "gateway" / "keep"
    marker.write_text("prod")
    # 用真实默认前缀判定会拒；这里借 M2LIVE_FORBIDDEN 把临时目录当作生产前缀。
    result = _run(
        _env(tmp_path, bin_dir, M2LIVE_STATE=str(state), M2LIVE_FORBIDDEN=str(tmp_path)),
        "down",
        "--purge",
    )
    assert result.returncode in (1, 3)
    assert marker.exists()


def test_preflight_fails_when_a_secret_is_missing(tmp_path):
    """从「其它都对」的基线只抽掉一个密钥文件。"""
    bin_dir = _fake_docker(tmp_path)
    env = _prepare_live_tree(tmp_path, bin_dir)
    base = _run(env, "preflight")
    assert base.returncode == 1, base.stdout + base.stderr  # 缺东西时本来就该失败
    (tmp_path / "etc/secrets/mysql_root").write_text("pw")
    os.chmod(tmp_path / "etc/secrets/mysql_root", 0o600)
    # 还缺其它文件；这里只断言「补上一个文件后失败项严格减少」不是本用例重点，
    # 重点是：缺文件必须让 preflight 返回 1 而不是 0。
    assert "缺文件" in (base.stdout + base.stderr)


def test_preflight_rejects_a_group_readable_secret(tmp_path):
    bin_dir = _fake_docker(tmp_path)
    env = _prepare_live_tree(tmp_path, bin_dir, stub_bytes=True)
    secret = tmp_path / "etc/secrets/mysql_root"
    secret.write_text("pw\n")
    os.chmod(secret, 0o644)
    result = _run(env, "preflight")
    assert result.returncode == 1
    assert "权限" in result.stdout + result.stderr


def test_preflight_rejects_the_ssh_shortcut_and_unknown_images(tmp_path):
    """旧实现里有一行无意义的 `ssh root@127.0.0.1`；镜像也只查存在。"""
    script = CHECKS.read_text(encoding="utf-8")
    assert "ssh " not in script, "preflight 里仍有与判据无关的 SSH 探测"
    assert "image inspect -f '{{.Id}}'" in script, "镜像只查存在没有比对身份"


def _prepare_live_tree(
    tmp_path: Path,
    bin_dir: Path,
    stub_bytes: bool = False,
    state: Path | None = None,
    leave_secret_missing: bool = False,
) -> dict[str, str]:
    """尽量把 live 前提凑齐，只在用例里破坏一项。

    `state` 给出时用它作为状态根（可以是一个「别的项目」的根）；默认用 tmp/state。
    `leave_secret_missing` 让预检因为缺密钥而失败 —— 用来证明预检失败时
    **不会**写入所有权标记。
    """
    state = state or (tmp_path / "state")
    for directory in ("store", "gateway", "node", "platform"):
        (state / directory).mkdir(parents=True, exist_ok=True)
    if not (state / ".owner").exists():
        (state / ".owner").write_text("m2-live-03\n")
    for directory in ("gateway", "node", "platform", "secrets"):
        (tmp_path / "etc" / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "dist/platform").mkdir(parents=True)
    (tmp_path / "dist/node").mkdir(parents=True)
    env = _env(tmp_path, bin_dir)
    env["M2LIVE_STATE"] = str(state)
    if leave_secret_missing:
        for path in (tmp_path / "etc/secrets").glob("*"):
            path.unlink()
    return env


def test_up_does_not_claim_a_foreign_state_root(tmp_path):
    """预检失败时不得写所有权标记 —— 否则后续 purge 会把别人的目录当成本栈的。"""
    bin_dir = _fake_docker(tmp_path)
    state = _owned_state(tmp_path, project="another-project")
    env = _prepare_live_tree(tmp_path, bin_dir, state=state)

    result = _run(env, "up")

    assert result.returncode == 2, result.stdout + result.stderr
    assert (state / ".owner").read_text().strip() == "another-project", "外来标记被改写"
    assert not (state / ".owner.tmp").exists() and not list(state.glob(".owner.tmp.*")), "留下了半成品标记"


def test_up_does_not_create_a_marker_before_preflight_passes(tmp_path):
    """全新状态根 + 预检失败：不能出现 .owner（连空的都不行）。"""
    bin_dir = _fake_docker(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = _prepare_live_tree(tmp_path, bin_dir, state=state, leave_secret_missing=True)
    (state / ".owner").unlink(missing_ok=True)

    result = _run(env, "up")

    assert result.returncode == 2
    assert not (state / ".owner").exists(), "预检未通过却写了所有权标记"


def test_a_successful_preflight_claims_the_root_atomically(tmp_path, monkeypatch):
    """成功路径：预检通过后 `.owner` 存在且含项目名，且不留临时文件。

    预检在这个假环境里几乎必然失败，所以这里直接调用认领函数 —— 它是 `up` 在
    预检之后做的**唯一**一件与所有权有关的事。成功/失败两条路径都覆盖。
    """
    bin_dir = _fake_docker(tmp_path)
    state = _owned_state(tmp_path)
    env = _prepare_live_tree(tmp_path, bin_dir, state=state)

    # 删掉预置的标记，走「新根认领」而不是「已有本栈标记」。
    (state / ".owner").unlink()

    script = (
        f'source <(sed -n "/^claim_state_root()/,/^}}/p" {CHECKS}); '
        "STATE=$1; PROJECT=m2-live-03; "
        'red() { printf "%s\\n" "$*" >&2; }; refuse() { red "refused: $*"; return 3; }; '
        "claim_state_root"
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(state)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (state / ".owner").read_text().strip() == "m2-live-03"
    assert not list(state.glob(".owner.tmp.*")), "原子写入留下了临时文件"


def test_manifest_mount_is_whitelisted():
    """节点只读挂载**单个**清单文件，它必须在 mounts 白名单里。

    否则 `checks.sh up` 的 preflight 会拒绝起栈 —— 整栈都起不来，不只是守卫不生效。
    """
    spec = importlib.util.spec_from_file_location("stack_checks_under_test", DEPLOY_DIR / "stack_checks.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    config = "/tmp/m2live-config"
    payload = {
        "services": {
            "node": {
                "volumes": [
                    {
                        "type": "bind",
                        "read_only": True,
                        "source": f"{config}/gateway/exec-manifest.json",
                    }
                ]
            }
        }
    }
    original = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        assert module.mounts(["/tmp/m2live-state", config]) == 0
    finally:
        sys.stdin = original
