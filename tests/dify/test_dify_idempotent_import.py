"""固定Dify镜像中的幂等导入和真实角色守卫。"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
IMAGE = "langgenius/dify-api@sha256:ceede5b903afaa20348f7ad80ebf847379dc56d886a99b9ec6a08913dacd7732"
TARGET = "/app/api/controllers/console/app/app_import.py"


def test_idempotent_import_with_upstream_role_guards(tmp_path):
    if shutil.which("docker") is None:
        assert not os.environ.get("CI"), "CI必须实际执行Dify角色回归"
        pytest.skip("需要本机Dify镜像")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        subprocess.run(["docker", "pull", IMAGE], check=True, capture_output=True, timeout=300)
    source = subprocess.check_output(["docker", "run", "--rm", "--entrypoint", "cat", IMAGE, TARGET])
    assert (
        hashlib.sha256(source).hexdigest()
        == "6e25dc4b1563a495e3cb9ab54dbf98a5524090005dd9bfbb91616f650bd10229"
    )
    file = tmp_path / "controllers/console/app/app_import.py"
    file.parent.mkdir(parents=True)
    file.write_bytes(source)
    subprocess.run(
        ["patch", "-p1", "--fuzz=0", "--batch", "-d", str(tmp_path)],
        input=(ROOT / "deploy/dify-36/patches/1.17.1/09-idempotent-app-import.patch").read_bytes(),
        check=True,
        capture_output=True,
    )
    # stdin提供合成用例，挂载只读候选；不初始化生产配置或网络。
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--entrypoint",
            "/app/api/.venv/bin/python",
            "-e",
            "PYTHONPATH=/app/api",
            "-e",
            "SECRET_KEY=synthetic-role-test-secret",
            "-v",
            f"{file}:{TARGET}:ro",
            IMAGE,
            "-",
        ],
        input=(ROOT / "tools/verification/verify_dify_idempotent_import.py").read_text(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
