"""初始化在 docker cp 不可用时传输配置，失败时保留原有宿主文件。"""

import fcntl
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/m2-live-03"


@pytest.fixture
def transfer(tmp_path):
    config, container, bin_dir = (tmp_path / name for name in ("config", "container", "bin"))
    for name in ("gateway", "node", "secrets", "model-proxy"):
        (config / name).mkdir(parents=True)
    (container / "tmp").mkdir(parents=True)
    originals = {
        "gateway/agent-runs.json": '{"environment":"old"}',
        "node/node.env": "AIOPS_NODE_TOKEN=old-node\n",
        "secrets/platform_admin": "old-admin\n",
        "model-proxy/proxy_token": "synthetic-proxy-token\n",
    }
    for name, value in originals.items():
        (config / name).write_text(value)
        (config / name).chmod(0o600)
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + r"""
import os, pathlib, subprocess, sys
a = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_CONTAINER"])
if a[0] == "cp":
    sys.exit(72)  # 目标 daemon 的 cp/只读嵌套挂载故障。
if a[0] == "compose":
    sys.exit(0)
if "python" in a:
    cmd = a[a.index("python"):]
    if cmd[1] == "-c":
        cmd[-1] = str(root / cmd[-1].lstrip("/"))
        result = subprocess.run([sys.executable, *cmd[1:]], input=sys.stdin.buffer.read())
        if cmd[-1].endswith("bootstrap_platform.py"):
            import hashlib
            digest = hashlib.sha256(pathlib.Path(cmd[-1]).read_bytes()).hexdigest()
            (root / "bootstrap-input-sha").write_text(digest)
        sys.exit(result.returncode)
    assert (root / "tmp/model_api_key").read_text().strip() == "synthetic-proxy-token"
    admin = root.parent / "platform-password"
    proposed = (root / "tmp/platform_admin").read_text()
    if admin.exists():
        assert admin.read_text() == proposed
    else:
        admin.write_text(proposed)
    for name, value in {
        "agent-runs.out.json": '{"environment":"new"}',
        "node.env.out": "AIOPS_NODE_TOKEN=new-node\n",
    }.items():
        (root / "tmp" / name).write_text(value)
    sys.exit(0)
if "rm" in a:
    if os.environ.get("FAIL_CLEANUP"):
        sys.exit(18)
    for name in a[a.index("-f") + 1:]:
        (root / name.lstrip("/")).unlink(missing_ok=True)
    sys.exit(0)
if "cat" in a:
    source = a[-1]
    if os.environ.get("FAIL_SOURCE") == source:
        sys.stdout.write("partial")
        sys.exit(17)
    if os.environ.get("EMPTY_SOURCE") != source:
        sys.stdout.buffer.write((root / source.lstrip("/")).read_bytes())
    sys.exit(0)
sys.exit(0)
"""
    )
    docker.chmod(0o700)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "M2LIVE_CONFIG": str(config),
        "M2LIVE_UID": str(os.getuid()),
        "M2LIVE_GID": str(os.getgid()),
        "M2LIVE_MODEL_ENDPOINT": "https://model-proxy:9101",
        "M2LIVE_MODEL_ID": "synthetic-model",
        "FAKE_CONTAINER": str(container),
    }
    return config, container, originals, env


def run_bootstrap(env):
    return subprocess.run(["bash", str(DEPLOY / "bootstrap.sh")], env=env, capture_output=True, timeout=15)


def test_bootstrap_transfers_without_docker_cp_and_preserves_identity(transfer):
    config, container, _, env = transfer
    result = run_bootstrap(env)
    assert result.returncode == 0, result.stderr
    assert not list((container / "tmp").iterdir())
    assert (container / "bootstrap-input-sha").read_text() == hashlib.sha256(
        (DEPLOY / "bootstrap_platform.py").read_bytes()
    ).hexdigest()
    for name, expected in (
        ("gateway/agent-runs.json", '{"environment":"new"}'),
        ("node/node.env", "AIOPS_NODE_TOKEN=new-node\n"),
        ("secrets/platform_admin", "old-admin\n"),
    ):
        path = config / name
        assert path.read_text() == expected
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.getuid()
    assert b"synthetic-proxy-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["FAIL_SOURCE", "EMPTY_SOURCE"])
@pytest.mark.parametrize("source", ["agent-runs.out.json", "node.env.out"])
def test_failed_or_empty_download_preserves_all_originals(transfer, failure, source):
    config, _, originals, env = transfer
    env[failure] = "/tmp/" + source
    result = run_bootstrap(env)
    assert result.returncode != 0
    for name, value in originals.items():
        assert (config / name).read_text() == value


def test_first_rotation_survives_failed_transfer_and_gateway_replacement(transfer):
    config, container, _, env = transfer
    admin = config / "secrets/platform_admin"
    admin.unlink()
    env["FAIL_SOURCE"] = "/tmp/node.env.out"
    result = run_bootstrap(env)
    assert result.returncode != 0
    password = admin.read_text()
    assert password == (container.parent / "platform-password").read_text()
    assert admin.stat().st_mode & 0o777 == 0o600
    assert not list((container / "tmp").iterdir())
    shutil.rmtree(container)
    (container / "tmp").mkdir(parents=True)
    del env["FAIL_SOURCE"]
    assert run_bootstrap(env).returncode == 0
    assert admin.read_text() == password
    assert not list((container / "tmp").iterdir())


def test_admin_rotation_uses_host_persisted_password(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", DEPLOY / "bootstrap_platform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    password = tmp_path / "platform_admin"
    password.write_text("host-durable-password")
    monkeypatch.setattr(module, "ADMIN_FILE", str(password))
    state = {"password": "admin123"}
    monkeypatch.setattr(module, "login", lambda value: "token" if value == state["password"] else None)

    def change(path, body, headers, method):
        assert path == "/user/password" and method == "PUT"
        state["password"] = body["newPassword"]
        return 200, {"status": 1}

    monkeypatch.setattr(module, "call", change)
    assert module.ensure_admin_rotated() == "token"
    assert state["password"] == password.read_text().strip() == "host-durable-password"
    assert module.ensure_admin_rotated() == "token"


def test_cleanup_failure_cannot_report_success(transfer):
    _, _, _, env = transfer
    env["FAIL_CLEANUP"] = "1"
    result = run_bootstrap(env)
    assert result.returncode != 0
    assert "临时凭据清理失败" in result.stderr.decode()


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_concurrent_invocation_cannot_touch_active_transfer(transfer, args):
    config, container, originals, env = transfer
    sentinel = container / "tmp/node_token"
    sentinel.write_text("active-transfer")
    with (config / ".bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["bash", str(DEPLOY / "bootstrap.sh"), *args], env=env, capture_output=True, timeout=5
        )
    assert result.returncode == 3
    assert sentinel.read_text() == "active-transfer"
    for name, value in originals.items():
        assert (config / name).read_text() == value
