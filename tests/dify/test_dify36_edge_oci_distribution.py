"""Dify 36 双侧边车统一 ACR OCI 构建、身份隔离部署与回滚合同测试（ADR-0367）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EDGE_DIR = ROOT / "deploy/dify-36/edge"
DOCKERFILE = EDGE_DIR / "Dockerfile"
BUILD_SH = EDGE_DIR / "build-edge-image.sh"
PUBLISH_SH = EDGE_DIR / "publish-edge-image.sh"
DEPLOY_SH = EDGE_DIR / "deploy-edge-image.sh"
WORKFLOW_YML = ROOT / ".github/workflows/release-dify-edge-image.yml"
ARCH_DOC = ROOT / "deploy/dify-36/ARCHITECTURE.md"
ADR_DOC = ROOT / "docs/adr/0367-dify-edge-sidecars-acr-convergence.md"


def test_edge_shell_scripts_syntax_valid() -> None:
    for script in (BUILD_SH, PUBLISH_SH, DEPLOY_SH):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"{script.name} 语法检查失败: {res.stderr}"


def test_edge_dockerfile_pins_dependencies_and_non_root_permissions() -> None:
    df = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in df
    assert "cryptography==46.0.7" in df
    assert "chmod -R a+rX /opt/aiops-edge" in df
    assert "dify_console_bridge" in df
    assert "dify_login_proxy" in df
    assert "PYTHONPATH=/opt/aiops-edge" in df


def test_deploy_edge_image_validation_and_dry_run() -> None:
    valid_ref = f"qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-edge@sha256:{'b' * 64}"

    bad_tag = subprocess.run(
        [
            "bash",
            str(DEPLOY_SH),
            "--image",
            "qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-edge:latest",
        ],
        capture_output=True,
        text=True,
    )
    assert bad_tag.returncode != 0
    assert "@sha256" in bad_tag.stderr

    bad_service = subprocess.run(
        ["bash", str(DEPLOY_SH), "--image", valid_ref, "--service", "inner-api", "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert bad_service.returncode != 0

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
        "state_dir": "/opt/aiops-edge-deploy",
        "mode": "deploy",
        "service": "all",
        "services": ["bridge", "login-proxy"],
        "image_ref": valid_ref,
    }

    rb = subprocess.run(
        ["bash", str(DEPLOY_SH), "--rollback", "--service", "bridge", "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    rb_plan = json.loads(rb.stdout)
    assert rb_plan["mode"] == "rollback"
    assert rb_plan["services"] == ["bridge"]
    assert rb_plan["image_ref"] is None


def test_publish_edge_image_allow_skip_guard() -> None:
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AIOPS_DIFY_EDGE_IMAGE_REPO",
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
    assert "跳过 Dify Edge ACR" in skipped.stdout

    strict = subprocess.run(
        ["bash", str(PUBLISH_SH)],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert strict.returncode != 0


def test_release_dify_edge_workflow_contract_and_docs() -> None:
    wf = yaml.safe_load(WORKFLOW_YML.read_text(encoding="utf-8"))
    on_block = wf[True] if True in wf else wf["on"]
    assert on_block["push"]["branches"] == ["main"]
    assert "deploy/dify-36/edge/**" in on_block["push"]["paths"]
    assert "services/dify-console-bridge/**" in on_block["push"]["paths"]
    assert "services/dify-login-proxy/**" in on_block["push"]["paths"]
    assert "workflow_dispatch" in on_block

    job = wf["jobs"]["publish-dify-edge"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout_step = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout_step["with"]["persist-credentials"] is False
    run_step = next(s for s in job["steps"] if "publish-edge-image.sh" in str(s.get("run", "")))
    assert "AIOPS_DIFY_EDGE_IMAGE_REPO" in run_step["env"]
    assert "AIOPS_ACR_USERNAME" in run_step["env"]
    assert "AIOPS_ACR_PASSWORD" in run_step["env"]

    arch_text = ARCH_DOC.read_text(encoding="utf-8")
    assert "aiops-dify-edge" in arch_text
    assert "aiopsbridge" in arch_text
    assert "aiopslogin" in arch_text
    assert "aiops-inner-api-forward" in arch_text
    assert "deploy-edge-image.sh" in arch_text

    assert ADR_DOC.is_file()
    for readme in (
        ROOT / "deploy/dify-36/README.md",
        ROOT / "ops/dify-console-bridge/README.md",
        ROOT / "ops/dify-login-proxy/README.md",
        ROOT / "ops/dify-inner-api/README.md",
    ):
        assert "ARCHITECTURE.md" in readme.read_text(encoding="utf-8")


def test_deploy_edge_isolates_identities_and_rolls_back_on_probe_failure(tmp_path: Path) -> None:
    new_ref = f"registry.example.test/release/edge@sha256:{'c' * 64}"
    state_dir = tmp_path / "edge-state"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "ops.log"

    stubs = {
        "dev-host": '#!/usr/bin/env bash\nexec bash -c "${@: -1}"\n',
        "id": """#!/usr/bin/env bash
set -eu
flag="$1"; user="$2"
case "${flag}:${user}" in
  -u:aiopsbridge) echo 992 ;;
  -g:aiopsbridge) echo 991 ;;
  -u:aiopslogin)  echo 991 ;;
  -g:aiopslogin)  echo 990 ;;
  *) exit 99 ;;
esac
""",
        "systemctl": """#!/usr/bin/env bash
set -eu
echo "systemctl $*" >> "$OPS_LOG"
case "$1" in
  is-active|is-enabled) exit 0 ;;
  disable|enable) exit 0 ;;
  *) exit 0 ;;
esac
""",
        "docker": """#!/usr/bin/env bash
set -eu
case "$1" in
  pull) echo "docker pull $2" >> "$OPS_LOG"; exit 0 ;;
  rm) echo "docker rm $*" >> "$OPS_LOG"; exit 0 ;;
  inspect)
    if [[ "$*" == *"Config.Image"* ]]; then
      exit 1
    fi
    echo "true"
    ;;
  run)
    echo "docker run $*" >> "$OPS_LOG"
    exit 0
    ;;
  *) exit 99 ;;
esac
""",
        "curl": """#!/usr/bin/env bash
# Simulate bridge OK, login-proxy unhealthy -> trigger rollback
if [[ "$*" == *"10119"* ]]; then
  echo "502"
  exit 0
fi
if [[ "$*" == *"10118"* ]]; then
  echo "403"
  exit 0
fi
if [[ "$*" == *"10008/__aiops/publish"* ]]; then
  echo "403"
  exit 0
fi
if [[ "$*" == *"10008/aiops/environments"* ]]; then
  echo "307"
  exit 0
fi
echo "502"
""",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name, script in stubs.items():
        stub = bin_dir / name
        stub.write_text(script, encoding="utf-8")
        stub.chmod(0o755)

    res = subprocess.run(
        ["bash", str(DEPLOY_SH), "--image", new_ref, "--state-dir", str(state_dir)],
        env={"PATH": f"{bin_dir}:{os.defpath}", "OPS_LOG": str(log_file)},
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "AUTO_ROLLED_BACK" in res.stderr

    log_txt = log_file.read_text(encoding="utf-8")
    # Strict runtime isolation checks for both containers
    assert "--name aiops-dify-console-bridge" in log_txt
    assert "--user 992:991" in log_txt
    assert "--env-file /etc/aiops-console-bridge/bridge.env" in log_txt
    assert "-v /var/lib/aiops-console-bridge:/var/lib/aiops-console-bridge:ro" in log_txt
    assert "--name aiops-dify-login-proxy" in log_txt
    assert "--user 991:990" in log_txt
    assert "--env-file /etc/aiops-login-proxy/login-proxy.env" in log_txt
    assert "-v /var/lib/aiops-login-proxy:/var/lib/aiops-login-proxy:rw" in log_txt
    # Legacy systemd rollback when migrating from systemd for the first time
    assert "systemctl enable --now aiops-dify-console-bridge.service" in log_txt
    assert "systemctl enable --now aiops-dify-login-proxy.service" in log_txt
