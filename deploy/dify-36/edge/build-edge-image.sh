#!/usr/bin/env bash
# 本地或 CI 构建并离线冒烟校验 aiops-dify-edge OCI 镜像（ADR-0367）。
set -euo pipefail
umask 027

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
cd "$REPO_ROOT"

IMAGE_TAG="${AIOPS_DIFY_EDGE_PATCHED_IMAGE:-aiops-dify-edge:local}"
VERSION="${AIOPS_EDGE_VERSION:-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')}"
REVISION="${AIOPS_EDGE_REVISION:-$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)}"

echo "== 1. 构建 aiops-dify-edge 镜像: $IMAGE_TAG (version=$VERSION rev=$REVISION)"
docker build \
  -f "$SCRIPT_DIR/Dockerfile" \
  --build-arg "AIOPS_EDGE_VERSION=$VERSION" \
  --build-arg "AIOPS_EDGE_REVISION=$REVISION" \
  -t "$IMAGE_TAG" \
  "$REPO_ROOT"

echo "== 2. 非 root 只读容器离线冒烟校验（aiopsbridge 992:991 & aiopslogin 991:990）"
docker run --rm --network none --user 992:991 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --entrypoint python "$IMAGE_TAG" -c '
import dify_console_bridge.__main__
from dify_console_bridge import bridge, client, config, environments, self_service_bridge, workflows
from pathlib import Path
assets = Path("/opt/aiops-edge/assets")
for name in ("dify-login-page.html", "dify-console-10008.conf", "dify-console-publish.conf", "dify-login-proxy.conf"):
    assert (assets / name).is_file(), f"missing asset: {name}"
print("smoke bridge ok")
'

docker run --rm --network none --user 991:990 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --entrypoint python "$IMAGE_TAG" -c '
import dify_login_proxy.__main__
from dify_login_proxy import company, core, environments, provision, server, service
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from aiops_diagnostics.onboarding.registry import EnvironmentRegistry
from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi
from aiops_diagnostics.dify.dify_workspace_directory import GatewayWorkspaceClient
print("smoke login-proxy ok")
'

echo "BUILD_EDGE_OK image=$IMAGE_TAG version=$VERSION revision=$REVISION"
