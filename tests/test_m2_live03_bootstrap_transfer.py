"""初始化在 docker cp 不可用时传输配置，失败时保留原有宿主文件。"""

import os
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
        sys.exit(subprocess.run([sys.executable, *cmd[1:]], input=sys.stdin.buffer.read()).returncode)
    assert (root / "tmp/model_api_key").read_text().strip() == "synthetic-proxy-token"
    for name, value in {
        "platform_admin": "new-admin\n",
        "agent-runs.out.json": '{"environment":"new"}',
        "node.env.out": "AIOPS_NODE_TOKEN=new-node\n",
    }.items():
        (root / "tmp" / name).write_text(value)
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
    assert (container / "tmp/bootstrap_platform.py").read_bytes() == (
        DEPLOY / "bootstrap_platform.py"
    ).read_bytes()
    for name, expected in (
        ("gateway/agent-runs.json", '{"environment":"new"}'),
        ("node/node.env", "AIOPS_NODE_TOKEN=new-node\n"),
        ("secrets/platform_admin", "new-admin\n"),
    ):
        path = config / name
        assert path.read_text() == expected
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.getuid()
    assert b"synthetic-proxy-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["FAIL_SOURCE", "EMPTY_SOURCE"])
@pytest.mark.parametrize("source", ["platform_admin", "agent-runs.out.json", "node.env.out"])
def test_failed_or_empty_download_preserves_all_originals(transfer, failure, source):
    config, _, originals, env = transfer
    env[failure] = "/tmp/" + source
    result = run_bootstrap(env)
    assert result.returncode != 0
    for name, value in originals.items():
        assert (config / name).read_text() == value
