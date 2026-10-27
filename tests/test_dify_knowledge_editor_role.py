"""真实Dify权限装饰器必须允许知识库选择，且不授予通用插件权限。"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
IMAGE = "langgenius/dify-api:1.17.1"
TARGET = "/app/api/controllers/console/workspace/plugin.py"


def test_editor_knowledge_options_with_upstream_role_guards(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("需要本机Dify镜像")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip("需要本机Dify1.17.1镜像；隔离验收另执行")
    source = subprocess.check_output(["docker", "run", "--rm", "--entrypoint", "cat", IMAGE, TARGET])
    file = tmp_path / "controllers/console/workspace/plugin.py"
    file.parent.mkdir(parents=True)
    file.write_bytes(source)
    subprocess.run(
        ["patch", "-p1", "--fuzz=0", "--batch", "-d", str(tmp_path)],
        input=(
            ROOT / "deploy/dify-36/patches/1.17.1/08-consumer-knowledge-editor-options.patch"
        ).read_bytes(),
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
            "1000:1000",
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
        input=(ROOT / "tools/verify_dify_knowledge_role.py").read_text(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
