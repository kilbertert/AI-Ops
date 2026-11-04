"""真实构建脚本与摘要检查；Docker替身只记录构建得到的ID和标签。"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy/m2-live-03"
BASE = "python@sha256:" + "a" * 64


@pytest.fixture
def build_repo(tmp_path):
    repo = tmp_path / "repo"
    deploy = repo / "deploy/m2-live-03"
    deploy.mkdir(parents=True)
    for name in ("build-image.sh", "context_digest.py", "gateway.Dockerfile", "gateway-requirements.txt"):
        shutil.copy2(DEPLOY / name, deploy / name)
    for name in ("src", "services/model-proxy", "tools/verification/m2_live", "tools/experiments/m2_close"):
        (repo / name).mkdir(parents=True)
        (repo / name / "sample.py").write_text("# sample\n")
    (repo / "uv.lock").write_text("locked-dependencies\n")
    lock = deploy / "images.lock.json"
    lock.write_text(json.dumps({"gateway_image": {"base_digest": BASE}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "a=sys.argv[1:]; p=pathlib.Path(os.environ['BUILD_IMAGE_RECORD'])\n"
        "if a[0]=='build':\n"
        " labels=dict(a[i+1].split('=',1) for i,v in enumerate(a) if v=='--label')\n"
        " p.write_text(json.dumps({'Id':'sha256:'+labels['m2live.source'], 'Config':{'Labels':labels}}))\n"
        "elif a[:2]==['image','inspect']:\n"
        " fmt=a[a.index('-f')+1] if '-f' in a else ''\n"
        " if 'RepoDigests' in fmt: print(os.environ['BUILD_BASE'])\n"
        " elif '.Id' in fmt: print(json.loads(p.read_text())['Id'])\n"
        " else: print('['+p.read_text()+']')\n"
        "else: sys.exit(2)\n"
    )
    docker.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "M2LIVE_BASE_IMAGE": BASE,
        "BUILD_BASE": BASE,
        "BUILD_IMAGE_RECORD": str(tmp_path / "image.json"),
    }
    return repo, deploy, env


def test_parallel_builds_keep_input_lock_and_bind_each_artifact(build_repo, tmp_path):
    repo, deploy, env = build_repo
    lock = deploy / "images.lock.json"
    original = lock.read_bytes()

    def build(name):
        task = tmp_path / name
        shutil.copytree(repo, task)
        (task / f"src/{name}.py").write_text(f"# {name}\n")
        task_deploy = task / "deploy/m2-live-03"
        image_record = task / "dist/image.json"
        image_record.parent.mkdir()
        run = subprocess.run(
            ["bash", str(task_deploy / "build-image.sh")],
            cwd=task,
            env={
                **env,
                "M2LIVE_DIST": str(task / "dist"),
                "BUILD_IMAGE_RECORD": str(image_record),
                "M2LIVE_GATEWAY_IMAGE": "aiops-m2-live-03-gateway:" + name,
            },
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert (task_deploy / "images.lock.json").read_bytes() == original
        identity = task / "dist/gateway-image/identity.json"
        value = json.loads(identity.read_text())
        assert value["image_id"] == json.loads(image_record.read_text())["Id"]
        assert value["uv_lock_sha256"] == hashlib.sha256((task / "uv.lock").read_bytes()).hexdigest()
        check = subprocess.run(
            [sys.executable, str(task_deploy / "context_digest.py"), "--check", str(identity)],
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr
        return task, identity

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(build, ("task-a", "task-b")))
    assert lock.read_bytes() == original
    assert (
        json.loads(first[1].read_text())["source_sha256"]
        != json.loads(second[1].read_text())["source_sha256"]
    )
    script = second[0] / "deploy/m2-live-03/context_digest.py"
    stale = subprocess.run([sys.executable, str(script), "--check", str(first[1])], capture_output=True)
    assert stale.returncode == 1
    (second[0] / "uv.lock").write_text("new-dependencies\n")
    stale = subprocess.run([sys.executable, str(script), "--check", str(second[1])], capture_output=True)
    assert stale.returncode == 1


@pytest.mark.parametrize("fault", ["retired_flag", "wrong_base", "build_failed", "source_changed"])
def test_refused_build_keeps_lock_and_existing_record(build_repo, tmp_path, fault):
    repo, deploy, env = build_repo
    identity = repo / "dist/gateway-image/identity.json"
    identity.parent.mkdir(parents=True)
    identity.write_text("previous-artifact")
    lock = deploy / "images.lock.json"
    original = lock.read_bytes()
    arguments = ["--update-lock"] if fault == "retired_flag" else []
    if fault == "wrong_base":
        env["BUILD_BASE"] = "python@sha256:" + "b" * 64
    if fault == "build_failed":
        docker = tmp_path / "bin/docker"
        docker.write_text(docker.read_text().replace("if a[0]=='build':", "if a[0]=='build':\n sys.exit(1)"))
    if fault == "source_changed":
        docker = tmp_path / "bin/docker"
        docker.write_text(
            docker.read_text().replace(
                "if a[0]=='build':",
                "if a[0]=='build':\n pathlib.Path('src/sample.py').write_text('changed during build')",
            )
        )
    result = subprocess.run(
        ["bash", str(deploy / "build-image.sh"), *arguments],
        cwd=repo,
        env={**env, "M2LIVE_DIST": str(repo / "dist")},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert lock.read_bytes() == original
    assert identity.read_text() == "previous-artifact"


@pytest.mark.parametrize("contents", [None, "not-json", "null", "[]", "{}"])
def test_context_check_requires_a_build_record(build_repo, tmp_path, contents):
    _, deploy, _ = build_repo
    identity = tmp_path / "missing-or-invalid.json"
    if contents is not None:
        identity.write_text(contents)
    result = subprocess.run(
        [sys.executable, str(deploy / "context_digest.py"), "--check", str(identity)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
