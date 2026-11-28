"""Dify Web 前端镜像 ACR 构建发布、36 不可变 @sha256 部署与回滚合同测试（ADR-0365）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
