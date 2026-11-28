"""Dify 36 双侧边车统一 ACR OCI 构建、身份隔离部署与回滚合同测试（ADR-0367）。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
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
    assert "FROM node:22-slim AS console-builder" in df
    assert "COPY --from=console-builder /build/dist /opt/aiops-edge/console-dist" in df


def test_decoupled_admin_console_bridge_and_web_patch_slimdown(tmp_path: Path) -> None:
    from dify_console_bridge.bridge import make_server
    from dify_console_bridge.config import BridgeSettings

    dist_dir = tmp_path / "console-dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script src="/aiops/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")

    settings = BridgeSettings(
        listen_host="127.0.0.1",
        listen_port=0,
        dify_base_url="http://127.0.0.1:10018",
        gateway_base_url="https://api.example.test",
        publish_key="x" * 32,
        console_origin="https://dify.ranlei.work",
        workspace_id="2db44904-f981-42ca-b7e2-6f35cb2afffd",
        console_dist_dir=str(dist_dir),
    )
    server = make_server(settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        # 1. 无 Cookie 访问 /aiops/environments 返回 307 -> /signin?redirect=%2Faiops%2Fenvironments
        try:
            opener.open(f"{base}/aiops/environments")
            raise AssertionError("expected 307 redirect")
        except urllib.error.HTTPError as exc:
            assert exc.code == 307
            assert exc.headers["Location"] == "/signin?redirect=%2Faiops%2Fenvironments"

        # 2. 带 Dify 会话 Cookie 返回 200 HTML 与安全头
        req = urllib.request.Request(
            f"{base}/aiops/environments",
            headers={"Cookie": "__Host-access_token=tok-123"},
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert '<div id="root"></div>' in resp.read().decode("utf-8")

        # 3. 静态 JS 产物返回 200 application/javascript
        with urllib.request.urlopen(f"{base}/aiops/assets/app.js") as resp:
            assert resp.status == 200
            assert "application/javascript" in resp.headers["Content-Type"]
            assert "console.log('ok')" in resp.read().decode("utf-8")

        # 4. 路径穿越必须拒绝（404）
        for bad_url in (
            f"{base}/aiops/assets/../secret.txt",
            f"{base}/aiops/assets/%2e%2e/secret.txt",
            f"{base}/aiops/assets/../../etc/passwd",
        ):
            try:
                urllib.request.urlopen(bad_url)
                raise AssertionError(f"expected 404 for {bad_url}")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()

    publish_conf = (ROOT / "ops/dify-console-bridge/dify-console-publish.conf").read_text(encoding="utf-8")
    assert "location ^~ /aiops {" in publish_conf

    adr_368 = ROOT / "docs/adr/0368-decouple-aiops-admin-console-from-dify-web.md"
    assert adr_368.is_file()

    web_1171 = ROOT / "deploy/dify-36/web-patches/1.17.1"
    for removed in (
        "aiops-environments.tsx",
        "aiops-environment-releases.tsx",
        "aiops-onboarding-wizard.tsx",
        "aiops-environments.browser.spec.tsx",
    ):
        assert not (web_1171 / removed).exists(), f"已迁出的管理台文件仍残留于 web-patches: {removed}"


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
