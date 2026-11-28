#!/usr/bin/env bash
# 在 36 宿主从公司 ACR 拉取不可变 @sha256 Dify Web 镜像并安全重启 web（含失败自动回滚）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  deploy-web-image.sh --image <repo>@sha256:<64hex> [--target 36] [--deploy-dir /opt/dify/deploy] [--dry-run]
  deploy-web-image.sh --rollback [--target 36] [--deploy-dir /opt/dify/deploy] [--dry-run]

说明:
  - 仅接受不可变 @sha256 镜像摘要（拒绝 :latest/:1.17.1 等可变标签）。
  - 仅更新 .env 中的 DIFY_WEB_IMAGE 并 --force-recreate web，不改动 api/worker/nginx/数据卷。
  - 若 127.0.0.1:10008/signin 与 /aiops/environments 探活失败，自动回滚至上一版本镜像。
EOF
  exit 2
}

IMAGE_REF=""
ROLLBACK=0
TARGET="36"
DEPLOY_DIR="/opt/dify/deploy"
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
    --target)
      [ "$#" -ge 2 ] || usage
      TARGET="$2"
      shift 2
      ;;
    --deploy-dir)
      [ "$#" -ge 2 ] || usage
      DEPLOY_DIR="$2"
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

if ! [[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "部署目录路径非法: $DEPLOY_DIR" >&2
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
  python3 - "$TARGET" "$DEPLOY_DIR" "$MODE" "$IMAGE_REF" <<'PY'
import json
import sys

target, deploy_dir, mode, image_ref = sys.argv[1:5]
print(json.dumps({
    "dry_run": True,
    "target": target,
    "deploy_dir": deploy_dir,
    "mode": mode,
    "image_ref": image_ref or None,
}, ensure_ascii=False, indent=2))
PY
  exit 0
fi

remote_cmd=$(printf 'bash -s -- %q %q %q' "$DEPLOY_DIR" "$MODE" "$IMAGE_REF")

dev-host exec "$TARGET" --allow-service-exec --timeout 300 -- "$remote_cmd" <<'REMOTE_EOF'
set -euo pipefail
umask 027

DEPLOY_DIR="$1"
MODE="$2"
TARGET_IMAGE="$3"

cd "$DEPLOY_DIR"
STATE_FILE="$DEPLOY_DIR/.dify-web-release.json"

cur_image=$(python3 - "$DEPLOY_DIR/.env" <<'PY'
import sys
p = sys.argv[1]
try:
    for line in open(p, encoding="utf-8"):
        if line.startswith("DIFY_WEB_IMAGE="):
            print(line.split("=", 1)[1].strip())
            break
except FileNotFoundError:
    pass
PY
)

if [ "$MODE" = "rollback" ]; then
  TARGET_IMAGE=$(python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit("ROLLBACK_STATE_MISSING")
prev = str(data.get("previous_image") or "").strip()
if not prev:
    raise SystemExit("ROLLBACK_PREVIOUS_IMAGE_MISSING")
print(prev)
PY
)
fi

if [ -z "$TARGET_IMAGE" ]; then
  echo "目标镜像为空，终止执行" >&2
  exit 1
fi

echo "== 1. 拉取并只读冒烟检查目标镜像: $TARGET_IMAGE"
docker pull "$TARGET_IMAGE"
docker run --rm --network none --entrypoint sh "$TARGET_IMAGE" -c \
  'test -f /app/targets/next/web/server.js && test -x /app/entrypoint.sh'

echo "== 2. 备份 .env 并切换 DIFY_WEB_IMAGE"
ts=$(date -u +%Y%m%dT%H%M%SZ)
bak_env="$DEPLOY_DIR/.env.bak-web-$ts"
cp -p "$DEPLOY_DIR/.env" "$bak_env"

restore_prev() {
  echo "!! 探活失败，正在自动恢复上一个 DIFY_WEB_IMAGE" >&2
  cp -p "$bak_env" "$DEPLOY_DIR/.env"
  chmod 600 "$DEPLOY_DIR/.env"
  docker compose up -d --force-recreate web
  echo "AUTO_ROLLED_BACK" >&2
  exit 1
}

python3 - "$DEPLOY_DIR/.env" "$TARGET_IMAGE" <<'PY'
import os, sys
from pathlib import Path

p = Path(sys.argv[1])
target_img = sys.argv[2]
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
found = False
for line in lines:
    if line.startswith("DIFY_WEB_IMAGE="):
        out.append(f"DIFY_WEB_IMAGE={target_img}\n")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"DIFY_WEB_IMAGE={target_img}\n")
tmp = p.with_name(".env.next-web")
tmp.write_text("".join(out), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY

echo "== 3. 仅重建 web 容器并做本机端点探活"
docker compose up -d --force-recreate web

ok=0
for _ in $(seq 1 30); do
  code_signin=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:10008/signin" || true)
  code_env=$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:10008/aiops/environments" || true)
  if [ "$code_signin" = "200" ] && [ "$code_env" = "307" ]; then
    ok=1
    break
  fi
  sleep 1
done

if [ "$ok" != "1" ]; then
  restore_prev
fi

python3 - "$STATE_FILE" "$TARGET_IMAGE" "$cur_image" "$MODE" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

state_p = Path(sys.argv[1])
target_img, old_img, mode = sys.argv[2:5]
prev_img = ""
if mode != "rollback" and old_img and old_img != target_img:
    prev_img = old_img
payload = {
    "schema": "aiops-dify-web-release/1",
    "current_image": target_img,
    "previous_image": prev_img,
    "mode": mode,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
tmp = state_p.with_name(".dify-web-release.next.json")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, state_p)
PY

echo "== 4. 当前容器与回滚说明"
docker ps --format "{{.Names}} {{.Image}} {{.Status}}" | grep -E "web|api|worker|nginx"
echo "回滚命令: bash deploy/dify-36/web-patches/deploy-web-image.sh --rollback"
REMOTE_EOF
