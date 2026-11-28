"""Dify Web 前端镜像 ACR 构建发布、36 不可变 @sha256 部署与回滚合同测试（ADR-0365）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WEB_PATCHES = ROOT / "deploy/dify-36/web-patches"
BUILD_SH = WEB_PATCHES / "build-web-image.sh"
PUBLISH_SH = WEB_PATCHES / "publish-web-image.sh"
DEPLOY_SH = WEB_PATCHES / "deploy-web-image.sh"
WORKFLOW_YML = ROOT / ".github/workflows/release-dify-web-image.yml"


def test_shell_scripts_syntax_valid() -> None:
    for script in (BUILD_SH, PUBLISH_SH, DEPLOY_SH):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"{script.name} 语法检查失败: {res.stderr}"


def test_deploy_web_image_enforces_immutable_digest_and_supports_dry_run() -> None:
    valid_ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web@sha256:{'a' * 64}"

    # 可变标签必须被拒绝
    bad_tag = subprocess.run(
        [
            "bash",
            str(DEPLOY_SH),
            "--image",
            "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web:1.17.1",
        ],
        capture_output=True,
        text=True,
    )
    assert bad_tag.returncode != 0
    assert "@sha256" in bad_tag.stderr

    # 非法主机目标名必须被拒绝
    bad_target = subprocess.run(
        ["bash", str(DEPLOY_SH), "--image", valid_ref, "--target", "36;rm -rf /", "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert bad_target.returncode != 0

    # 合法 @sha256 dry-run 输出结构化计划
    good = subprocess.run(
        ["bash", str(DEPLOY_SH), "--image", valid_ref, "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    plan = json.loads(good.stdout)
    assert plan == {
        "dry_run": True,
        "target": "36",
        "deploy_dir": "/opt/dify/deploy",
        "mode": "deploy",
        "image_ref": valid_ref,
    }

    # --rollback dry-run 无需 --image
    rb = subprocess.run(
        ["bash", str(DEPLOY_SH), "--rollback", "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    rb_plan = json.loads(rb.stdout)
    assert rb_plan["mode"] == "rollback"
    assert rb_plan["image_ref"] is None


def test_publish_web_image_allow_skip_guard() -> None:
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AIOPS_DIFY_WEB_IMAGE_REPO",
            "AIOPS_ACR_USERNAME",
            "AIOPS_ACR_PASSWORD",
        }
    }

    skipped = subprocess.run(
        ["bash", str(PUBLISH_SH), "--allow-skip-unconfigured"],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert skipped.returncode == 0
    assert "跳过 Dify Web ACR" in skipped.stdout

    strict = subprocess.run(
        ["bash", str(PUBLISH_SH)],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert strict.returncode != 0


def test_release_dify_web_workflow_contract_and_docs() -> None:
    wf = yaml.safe_load(WORKFLOW_YML.read_text(encoding="utf-8"))
    on_block = wf[True] if True in wf else wf["on"]
    assert on_block["push"]["branches"] == ["main"]
    assert "deploy/dify-36/web-patches/**" in on_block["push"]["paths"]
    assert "workflow_dispatch" in on_block

    job = wf["jobs"]["publish-dify-web"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout_step = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout_step["with"]["persist-credentials"] is False
    run_step = next(s for s in job["steps"] if "publish-web-image.sh" in str(s.get("run", "")))
    assert "AIOPS_DIFY_WEB_IMAGE_REPO" in run_step["env"]
    assert "AIOPS_ACR_USERNAME" in run_step["env"]
    assert "AIOPS_ACR_PASSWORD" in run_step["env"]

    for doc in (ROOT / "deploy/dify-36/README.md", WEB_PATCHES / "README.md"):
        txt = doc.read_text(encoding="utf-8")
        assert "publish-web-image.sh" in txt
        assert "deploy-web-image.sh" in txt


@pytest.mark.parametrize("restore_status", [0, 1])
def test_deploy_restores_previous_env_when_compose_recreate_fails(
    tmp_path: Path, restore_status: int
) -> None:
    old_ref = f"registry.example.test/release/web@sha256:{'a' * 64}"
    new_ref = f"registry.example.test/release/web@sha256:{'b' * 64}"
    old_env = f"DIFY_WEB_IMAGE={old_ref}\nKEEP_ME=unchanged\n"
    env_file = tmp_path / ".env"
    env_file.write_text(old_env, encoding="utf-8")
    ledger = tmp_path / ".dify-web-release.json"
    old_ledger = json.dumps({"current_image": old_ref})
    ledger.write_text(old_ledger, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "dev-host": '#!/usr/bin/env bash\nexec bash -c "${@: -1}"\n',
        "docker": """#!/usr/bin/env bash
set -eu
case "$1" in
  pull|run) exit 0 ;;
  compose)
    [ "$*" = "compose up -d --force-recreate web" ] || exit 99
    if [ ! -f "$COMPOSE_LOG" ]; then
      grep '^DIFY_WEB_IMAGE=' .env > "$COMPOSE_LOG"
      exit 1
    fi
    grep '^DIFY_WEB_IMAGE=' .env >> "$COMPOSE_LOG"
    exit "$RESTORE_STATUS"
    ;;
  *) exit 99 ;;
esac
""",
        "curl": '#!/usr/bin/env bash\nprintf "unexpected health check\\n" >> "$COMPOSE_LOG"\nexit 99\n',
    }
    for name, script in stubs.items():
        stub = bin_dir / name
        stub.write_text(script, encoding="utf-8")
        stub.chmod(0o755)

    compose_log = tmp_path / "compose.log"
    result = subprocess.run(
        ["bash", str(DEPLOY_SH), "--image", new_ref, "--deploy-dir", str(tmp_path)],
        check=False,
        env={
            "PATH": f"{bin_dir}:{os.defpath}",
            "COMPOSE_LOG": str(compose_log),
            "RESTORE_STATUS": str(restore_status),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert env_file.read_text(encoding="utf-8") == old_env
    assert compose_log.read_text(encoding="utf-8").splitlines() == [
        f"DIFY_WEB_IMAGE={new_ref}",
        f"DIFY_WEB_IMAGE={old_ref}",
    ]
    assert ledger.read_text(encoding="utf-8") == old_ledger
    if restore_status:
        assert "AUTO_ROLLBACK_FAILED" in result.stderr
        assert "AUTO_ROLLED_BACK" not in result.stderr
    else:
        assert "AUTO_ROLLED_BACK" in result.stderr
