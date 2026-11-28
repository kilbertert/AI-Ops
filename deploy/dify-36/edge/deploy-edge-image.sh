#!/usr/bin/env bash
# 在 36 宿主从公司 ACR 拉取不可变 @sha256 aiops-dify-edge 镜像，并按双容器双身份隔离运行 sidecars（含失败自动回滚）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  deploy-edge-image.sh --image <repo>@sha256:<64hex> [--service all|bridge|login-proxy] [--target 36] [--state-dir /opt/aiops-edge-deploy] [--dry-run]
  deploy-edge-image.sh --rollback [--service all|bridge|login-proxy] [--target 36] [--state-dir /opt/aiops-edge-deploy] [--dry-run]

说明:
  - 仅接受不可变 @sha256 镜像摘要（拒绝 :latest 等可变标签）。
  - 同一份只读镜像拆为两个严格隔离的 Docker 容器：
      * aiops-dify-console-bridge (aiopsbridge / /etc/aiops-console-bridge/bridge.env / /var/lib/aiops-console-bridge:ro)
      * aiops-dify-login-proxy    (aiopslogin  / /etc/aiops-login-proxy/login-proxy.env / /var/lib/aiops-login-proxy:rw)
  - 首次从旧 systemd 单元切换时自动 disable --now 对应旧 unit；任一容器启动或探活失败自动回滚。
EOF
  exit 2
}

IMAGE_REF=""
ROLLBACK=0
SERVICE="all"
TARGET="36"
STATE_DIR="/opt/aiops-edge-deploy"
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image)
      [ "$#" -ge 2 ] || usage
      IMAGE_REF="$2"
      shift 2
      ;;
    --rollback)
      ROLLBACK=1
      shift
      ;;
    --service)
      [ "$#" -ge 2 ] || usage
      SERVICE="$2"
      shift 2
      ;;
    --target)
      [ "$#" -ge 2 ] || usage
      TARGET="$2"
      shift 2
      ;;
    --state-dir)
      [ "$#" -ge 2 ] || usage
      STATE_DIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      ;;
  esac
done

case "$SERVICE" in
  all|bridge|login-proxy) ;;
  *)
    echo "--service 仅支持 all|bridge|login-proxy，收到: $SERVICE" >&2
    exit 2
    ;;
esac

if [ "$ROLLBACK" = "1" ] && [ -n "$IMAGE_REF" ]; then
  echo "--rollback 与 --image 互斥，请二选一" >&2
  exit 2
fi
if [ "$ROLLBACK" = "0" ] && [ -z "$IMAGE_REF" ]; then
  usage
fi

if ! [[ "$TARGET" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "目标主机名非法: $TARGET" >&2
  exit 2
fi

if ! [[ "$STATE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "状态目录路径非法: $STATE_DIR" >&2
  exit 2
fi

if [ "$ROLLBACK" = "0" ]; then
  if ! [[ "$IMAGE_REF" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
    echo "镜像引用必须为不可变 @sha256:<64位小写hex> 摘要，拒绝可变标签: $IMAGE_REF" >&2
    exit 2
  fi
fi

MODE="deploy"
[ "$ROLLBACK" = "1" ] && MODE="rollback"

if [ "$DRY_RUN" = "1" ]; then
  python3 - "$TARGET" "$STATE_DIR" "$MODE" "$SERVICE" "$IMAGE_REF" <<'PY'
import json
import sys

target, state_dir, mode, service, image_ref = sys.argv[1:6]
services = ["bridge", "login-proxy"] if service == "all" else [service]
print(json.dumps({
    "dry_run": True,
    "target": target,
    "state_dir": state_dir,
    "mode": mode,
    "service": service,
    "services": services,
    "image_ref": image_ref or None,
}, ensure_ascii=False, indent=2))
PY
  exit 0
fi

remote_cmd=$(printf 'bash -s -- %q %q %q %q' "$STATE_DIR" "$MODE" "$SERVICE" "$IMAGE_REF")

dev-host exec "$TARGET" --allow-service-exec --timeout 300 -- "$remote_cmd" <<'REMOTE_EOF'
set -euo pipefail
umask 027

STATE_DIR="$1"
MODE="$2"
SERVICE_SEL="$3"
TARGET_IMAGE="$4"

install -d -m 0700 "$STATE_DIR"
STATE_FILE="$STATE_DIR/.dify-edge-release.json"

if [ "$SERVICE_SEL" = "all" ]; then
  SERVICES=("bridge" "login-proxy")
else
  SERVICES=("$SERVICE_SEL")
fi

read_state_images() {
  local svc="$1"
  python3 - "$STATE_FILE" "$svc" <<'PY'
import json, sys
p, svc = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(p, encoding="utf-8"))
except FileNotFoundError:
    print("")
    print("")
    raise SystemExit(0)
entry = (data.get("services") or {}).get(svc) or {}
print(str(entry.get("current_image") or "").strip())
print(str(entry.get("previous_image") or "").strip())
PY
}

resolve_service_target() {
  local svc="$1"
  local cur_img prev_img
  {
    IFS= read -r cur_img
    IFS= read -r prev_img
  } < <(read_state_images "$svc")
  if [ "$MODE" = "rollback" ]; then
    if [ -z "$prev_img" ]; then
      echo "ROLLBACK_PREVIOUS_IMAGE_MISSING:$svc" >&2
      exit 1
    fi
    printf '%s\n%s\n' "$prev_img" "$cur_img"
  else
    printf '%s\n%s\n' "$TARGET_IMAGE" "$cur_img"
  fi
}

BRIDGE_UID=$(id -u aiopsbridge)
BRIDGE_GID=$(id -g aiopsbridge)
LOGIN_UID=$(id -u aiopslogin)
LOGIN_GID=$(id -g aiopslogin)

declare -A SVC_TARGET_IMG
declare -A SVC_PREV_IMG
declare -A SVC_PREV_DOCKER
declare -A SVC_WAS_SYSTEMD

for svc in "${SERVICES[@]}"; do
  {
    IFS= read -r tgt
    IFS= read -r cur
  } < <(resolve_service_target "$svc")
  SVC_TARGET_IMG["$svc"]="$tgt"
  SVC_PREV_IMG["$svc"]="$cur"

  echo "== 1. 拉取并只读冒烟检查镜像 ($svc): $tgt"
  docker pull "$tgt"
  docker run --rm --network none --user "${BRIDGE_UID}:${BRIDGE_GID}" --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python "$tgt" -c \
    'import dify_console_bridge.__main__'
  docker run --rm --network none --user "${LOGIN_UID}:${LOGIN_GID}" --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python "$tgt" -c \
    'import dify_login_proxy.__main__, cryptography.hazmat.primitives.ciphers'
done

start_container() {
  local svc="$1"
  local img="$2"
  if [ "$svc" = "bridge" ]; then
    docker rm -f aiops-dify-console-bridge >/dev/null 2>&1 || true
    docker run -d \
      --name aiops-dify-console-bridge \
      --restart unless-stopped \
      --network host \
      --user "${BRIDGE_UID}:${BRIDGE_GID}" \
      --read-only \
      --tmpfs /tmp:rw,noexec,nosuid,size=32m \
      --security-opt no-new-privileges:true \
      --cap-drop ALL \
      --env-file /etc/aiops-console-bridge/bridge.env \
      -v /var/lib/aiops-console-bridge:/var/lib/aiops-console-bridge:ro \
      --entrypoint python \
      "$img" \
      -m dify_console_bridge >/dev/null
  else
    docker rm -f aiops-dify-login-proxy >/dev/null 2>&1 || true
    docker run -d \
      --name aiops-dify-login-proxy \
      --restart unless-stopped \
      --network host \
      --user "${LOGIN_UID}:${LOGIN_GID}" \
      --read-only \
      --tmpfs /tmp:rw,noexec,nosuid,size=32m \
      --security-opt no-new-privileges:true \
      --cap-drop ALL \
      --env-file /etc/aiops-login-proxy/login-proxy.env \
      -v /var/lib/aiops-login-proxy:/var/lib/aiops-login-proxy:rw \
      --entrypoint python \
      "$img" \
      -m dify_login_proxy >/dev/null
  fi
}

probe_service() {
  local svc="$1"
  local ok=0
  for _ in $(seq 1 30); do
    if [ "$svc" = "bridge" ]; then
      local running c1 c2 c_env
      running=$(docker inspect -f '{{.State.Running}}' aiops-dify-console-bridge 2>/dev/null || echo false)
      c1=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:10118/__aiops/publish" || true)
      c2=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:10008/__aiops/publish" || true)
      c_env=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:10008/aiops/environments" || true)
      if [ "$running" = "true" ] && [ "$c1" = "403" ] && [ "$c2" = "403" ] && [ "$c_env" = "307" ]; then
        ok=1
        break
      fi
    else
      local running c_dir c_tls c_signin
      running=$(docker inspect -f '{{.State.Running}}' aiops-dify-login-proxy 2>/dev/null || echo false)
      c_dir=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:10119/__aiops/login/environments" || true)
      c_tls=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:10008/__aiops/login" || true)
      c_signin=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:10008/signin" || true)
      if [ "$running" = "true" ] && [ "$c_dir" = "200" ] && [ "$c_tls" = "400" ] && [ "$c_signin" = "200" ]; then
        ok=1
        break
      fi
    fi
    sleep 1
  done
  [ "$ok" = "1" ]
}

rollback_all() {
  local reason="$1"
  echo "!! $reason，正在自动回滚本次切换的 sidecar" >&2
  for svc in "${SERVICES[@]}"; do
    local cname unit prev_docker prev_state was_sd
    if [ "$svc" = "bridge" ]; then
      cname="aiops-dify-console-bridge"
      unit="aiops-dify-console-bridge.service"
    else
      cname="aiops-dify-login-proxy"
      unit="aiops-dify-login-proxy.service"
    fi
    prev_docker="${SVC_PREV_DOCKER[$svc]:-}"
    prev_state="${SVC_PREV_IMG[$svc]:-}"
    was_sd="${SVC_WAS_SYSTEMD[$svc]:-0}"
    if [ -n "$prev_docker" ]; then
      start_container "$svc" "$prev_docker" || true
    elif [ -n "$prev_state" ]; then
      start_container "$svc" "$prev_state" || true
    elif [ "$was_sd" = "1" ]; then
      docker rm -f "$cname" >/dev/null 2>&1 || true
      systemctl enable --now "$unit" || true
    fi
  done
  echo "AUTO_ROLLED_BACK" >&2
  exit 1
}

echo "== 2. 记录切换前快照并切换至隔离容器"
for svc in "${SERVICES[@]}"; do
  if [ "$svc" = "bridge" ]; then
    cname="aiops-dify-console-bridge"
    unit="aiops-dify-console-bridge.service"
  else
    cname="aiops-dify-login-proxy"
    unit="aiops-dify-login-proxy.service"
  fi
  prev_docker=$(docker inspect -f '{{.Config.Image}}' "$cname" 2>/dev/null || true)
  SVC_PREV_DOCKER["$svc"]="$prev_docker"
  if systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit"; then
    SVC_WAS_SYSTEMD["$svc"]="1"
    systemctl disable --now "$unit"
  else
    SVC_WAS_SYSTEMD["$svc"]="0"
  fi

  if ! start_container "$svc" "${SVC_TARGET_IMG[$svc]}"; then
    rollback_all "容器 $cname 启动失败"
  fi
done

echo "== 3. 验证回环端点与同源入口阻断/探活"
for svc in "${SERVICES[@]}"; do
  if ! probe_service "$svc"; then
    rollback_all "$svc 探活失败"
  fi
done

echo "== 4. 写入不可变发布台账"
for svc in "${SERVICES[@]}"; do
  python3 - "$STATE_FILE" "$svc" "${SVC_TARGET_IMG[$svc]}" "${SVC_PREV_IMG[$svc]}" "$MODE" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

state_p = Path(sys.argv[1])
svc, target_img, old_img, mode = sys.argv[2:6]
try:
    data = json.loads(state_p.read_text(encoding="utf-8"))
except FileNotFoundError:
    data = {"schema": "aiops-dify-edge-release/1", "services": {}}

services = data.setdefault("services", {})
prev_img = old_img if (mode != "rollback" and old_img and old_img != target_img) else (
    "" if mode == "rollback" else str((services.get(svc) or {}).get("previous_image") or "")
)
services[svc] = {
    "current_image": target_img,
    "previous_image": prev_img,
    "mode": mode,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
data["schema"] = "aiops-dify-edge-release/1"
data["updated_at"] = services[svc]["updated_at"]
tmp = state_p.with_name(".dify-edge-release.next.json")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, state_p)
PY
done

echo "DEPLOY_EDGE_OK service=$SERVICE_SEL mode=$MODE"
REMOTE_EOF
