#!/usr/bin/env bash
# 构建隔离 Gateway 的可部署镜像，并输出它的精确身份。
#
# 只在开发机/runner 运行。服务宿主拿到的是 `docker save` 出来的 tar，不在宿主下载依赖。
#
# 身份 = 镜像 ID（sha256）+ 一个内容摘要标签：
#   m2live.source     构造上下文的 tar sha256（src + tools + requirements + Dockerfile）
#   m2live.lock       uv.lock 的 sha256（依赖范围的唯一来源）
#   m2live.base      基础镜像的 repo digest
# preflight 比对前两项，所以「把镜像重建成别的内容但打同一个 tag」会失败。
#
# 用法：
#   deploy/m2-live-03/build-image.sh                 # 构建并写字
#   deploy/m2-live-03/build-image.sh --save out.tar  # 另外导出可搬运的 tar
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$HERE/../.." && pwd)
IMAGE=${M2LIVE_GATEWAY_IMAGE:-aiops-m2-live-03-gateway:local}
BASE=${M2LIVE_BASE_IMAGE:-python:3.13-slim}
OUT=${M2LIVE_DIST:-/opt/aiops-m2-live}/gateway-image
SAVE=""
UPDATE_LOCK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --save) SAVE=$2; shift 2 ;;
    # 把这次构建的精确身份写回 images.lock.json。构建与记录是两件事：
    # 只有显式要求时才改仓库里被审查的那个文件。
    --update-lock) UPDATE_LOCK=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "uv 不在 PATH（依赖必须从 uv.lock 导出）" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker 不可用" >&2; exit 2; }

work=$(mktemp -d "${TMPDIR:-/tmp}/m2live-build-XXXXXX")
trap 'rm -rf "$work"' EXIT

cp "$HERE/gateway.Dockerfile" "$work/Dockerfile"
cp "$HERE/gateway-requirements.txt" "$work/gateway-requirements.txt"
# 源码同样用 tar 过滤：跑过测试的树上会有 `__pycache__`，把它算进内容摘要会让
# 「同一份源码」两次构建得到不同身份 —— 摘要必须只覆盖源码本身。
(cd "$REPO" && tar -cf - --exclude '__pycache__' --exclude '*.pyc' src) | (cd "$work" && tar -xf -)
# 只带运行需要的工具：节点源码不进 Gateway 镜像。
# 归档里的路径已经带 `tools/`，因此**解到构建上下文根**，不是解到 tools/ 里
# （曾经的写法产生 tools/tools/…，容器启动时才以「文件不存在」暴露）。
(cd "$REPO" && tar -cf - --exclude '__pycache__' --exclude '*.pyc' tools/m2_live tools/m2_close) \
  | (cd "$work" && tar -xf -)

# `LC_ALL=C` 不是风格问题：`sort` 默认按**本机 locale** 排序，同 141 个文件在
# `en_US.UTF-8` 与 `C` 下顺序不同，摘要因此不同（实测 7e6dc9ca… vs e7c9af0c…）。
# 锁文件里那个值必须与运行环境无关，否则每个 reviewer 算出自己的一个。
context_sha=$(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')
lock_sha=$(sha256sum "$REPO/uv.lock" | awk '{print $1}')
base_digest=$(docker image inspect -f '{{index .RepoDigests 0}}' "$BASE" 2>/dev/null || true)
[ -n "$base_digest" ] || { echo "基础镜像 $BASE 没有 RepoDigest：先拉取，别用本地构建的替身" >&2; exit 2; }

DOCKER_BUILDKIT=1 docker build \
  --build-arg M2LIVE_BASE_IMAGE="$BASE" \
  --label "m2live.source=$context_sha" \
  --label "m2live.lock=$lock_sha" \
  --label "m2live.base=$base_digest" \
  -t "$IMAGE" "$work"

image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE")
mkdir -p "$OUT"
cat > "$OUT/identity.json" <<JSON
{
  "image": "$IMAGE",
  "image_id": "$image_id",
  "source_sha256": "$context_sha",
  "uv_lock_sha256": "$lock_sha",
  "base_image": "$BASE",
  "base_digest": "$base_digest",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
cat "$OUT/identity.json"

if [ "$UPDATE_LOCK" -eq 1 ]; then
  python3 - "$HERE/images.lock.json" "$OUT/identity.json" <<'PY'
import json, sys
lock_path, identity_path = sys.argv[1], sys.argv[2]
lock = json.load(open(lock_path))
identity = json.load(open(identity_path))
section = lock.setdefault("gateway_image", {})
section.update(
    {
        "image": identity["image"].split(":")[0],
        "image_id": identity["image_id"],
        "source_sha256": identity["source_sha256"],
        "uv_lock_sha256": identity["uv_lock_sha256"],
        "base_image": identity["base_image"],
        "base_digest": identity["base_digest"],
        "built_at": identity["built_at"],
    }
)
section.setdefault(
    "_comment",
    "由 build-image.sh --update-lock 写入：source_sha256 是构造上下文的摘要，"
    "uv_lock_sha256 是 uv.lock 的摘要；preflight 两项都比对。",
)
open(lock_path, "w").write(json.dumps(lock, indent=2, ensure_ascii=False) + "\n")
print(f"updated {lock_path}")
PY
fi

if [ -n "$SAVE" ]; then
  docker save "$IMAGE" -o "$SAVE"
  sha256sum "$SAVE"
fi
