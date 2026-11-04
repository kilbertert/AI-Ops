#!/usr/bin/env bash
# 构建隔离 Gateway 的可部署镜像，并输出它的精确身份。
#
# 只在开发机/runner 运行。服务宿主拿到的是 `docker save` 出来的 tar，不在宿主下载依赖。
#
# 身份 = 镜像 ID（sha256）+ 一个内容摘要标签：
#   m2live.source     构造上下文内容摘要（src + tools + requirements + Dockerfile）
#   m2live.lock       uv.lock 的 sha256（依赖范围的唯一来源）
#   m2live.base      基础镜像的 repo digest
# preflight 比对三项与镜像ID；构建记录随制品交付，不改仓库输入锁。
#
# 用法：
#   deploy/m2-live-03/build-image.sh                 # 构建并输出记录
#   deploy/m2-live-03/build-image.sh --save out.tar  # 另外导出可搬运的 tar
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$HERE/../.." && pwd)
IMAGE=${M2LIVE_GATEWAY_IMAGE:-aiops-m2-live-03-gateway:local}
LOCKED_BASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_image"]["base_digest"])' "$HERE/images.lock.json")
BASE=${M2LIVE_BASE_IMAGE:-$LOCKED_BASE}
OUT=${M2LIVE_DIST:-$REPO/dist}/gateway-image
SAVE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --save) SAVE=$2; shift 2 ;;
    --update-lock) echo "--update-lock 已退役：构建记录只写入 $OUT/identity.json，仓库锁只维护输入" >&2; exit 2 ;;
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
(cd "$REPO" && tar -cf - --exclude '__pycache__' --exclude '*.pyc' --exclude 'node/target' --exclude 'm3node/build' tools/verification/m2_live tools/experiments/m2_close services/model-proxy) \
  | (cd "$work" && tar -xf -)

# `LC_ALL=C` 不是风格问题：`sort` 默认按**本机 locale** 排序，同 141 个文件在
# `en_US.UTF-8` 与 `C` 下顺序不同，摘要因此不同（实测 7e6dc9ca… vs e7c9af0c…）。
# 构建记录必须与运行环境无关，否则每个 reviewer 算出自己的一个。
context_sha=$(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')
lock_sha=$(sha256sum "$REPO/uv.lock" | awk '{print $1}')
base_digest=$(docker image inspect -f '{{index .RepoDigests 0}}' "$BASE" 2>/dev/null || true)
[ -n "$base_digest" ] || { echo "基础镜像 $BASE 没有 RepoDigest：先拉取，别用本地构建的替身" >&2; exit 2; }
[ "$base_digest" = "$LOCKED_BASE" ] || { echo "基础镜像摘要与输入锁不符" >&2; exit 2; }

DOCKER_BUILDKIT=1 docker build \
  --build-arg M2LIVE_BASE_IMAGE="$base_digest" \
  --label "m2live.source=$context_sha" \
  --label "m2live.lock=$lock_sha" \
  --label "m2live.base=$base_digest" \
  -t "$IMAGE" "$work"

image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE")
mkdir -p "$OUT"
python3 - "$OUT/identity.json" "$IMAGE" "$image_id" "$context_sha" "$lock_sha" "$BASE" "$base_digest" "$HERE/context_digest.py" <<'PY_IDENTITY'
import json, subprocess, sys
from datetime import UTC, datetime
from pathlib import Path
path = Path(sys.argv[1])
record = dict(zip(("image", "image_id", "source_sha256", "uv_lock_sha256", "base_image", "base_digest"), sys.argv[2:8], strict=True))
record["built_at"] = datetime.now(UTC).isoformat()
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
try:
    subprocess.run([sys.executable, sys.argv[8], "--check", str(temporary)], check=True)
    temporary.replace(path)
finally:
    temporary.unlink(missing_ok=True)
print(json.dumps(record, ensure_ascii=False, indent=2))
PY_IDENTITY

if [ -n "$SAVE" ]; then
  docker save "$IMAGE" -o "$SAVE"
  sha256sum "$SAVE"
fi
