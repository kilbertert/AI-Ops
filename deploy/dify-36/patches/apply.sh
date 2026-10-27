#!/usr/bin/env bash
# 把本目录的补丁打在上游镜像的**原文件**上，产出 api/worker 的只读挂载树。
#
# 为什么要这一步：Dify 1.17.1 在 36 上跑的是**未改动的上游镜像**，而这套实例需要
# 三处上游还没有的行为（见 README「上游补丁」）。改法不是重做镜像，也不是挂一份
# 手抄的整文件，而是：
#
#   镜像里的原文件 --sha256 核对--> 打本目录的 .patch --> ./volumes/app/patched-api/
#
# 核对是**前置条件**：镜像一换（升级 1.17.2），原文件 sha 就对不上，本脚本直接非零
# 退出，而不是把一份陈旧的补丁树挂上去静默运行。这时要重做的是补丁本身。
#
# 跑法（宿主、root、在 deploy/ 目录）：  ./patches/apply.sh
set -euo pipefail
cd "$(dirname "$0")/.."          # deploy/

IMAGE="${DIFY_API_IMAGE:-docker.1panel.live/langgenius/dify-api:1.17.1}"
VERSION_DIR="patches/1.17.1"
OUT="volumes/app/patched-api"

# 上游文件 → 期望 sha256（tag 1.17.1 = 8387590ace4a094de812b7847fc6a4c3a27cd52b）。
# 换镜像时这些值必须一起换，否则本脚本拒绝执行 —— 这正是它的用处。
PINNED="
controllers/console/datasets/hit_testing_base.py de59a1cd2390a8d04015b04320d0af4ffce564c13ba1635f111f4e8a53810004
controllers/service_api/dataset/hit_testing.py 4d56e1bab4585a7b5f892557097d9525b8b144d8cec22e6d999e02f569068cbd
services/external_knowledge_service.py 59afcc901649b2f64ecd4814f7b7d98fa1577371403578b4bb64e7227eb4f726
controllers/console/datasets/external.py f24bdd52739cad6207ed2957e94258be22e5e144bd84d2758882d797fdd9b013
services/dataset_service.py 93deb113f58ac79b1e3023257f7912c05196e23aa71a9626149c220904cf93e3
controllers/console/workspace/plugin.py 53367ae76ce6c6fd171308ae8adc4bba6ee7b29100fc26f4f478a3d991dc4536
"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

echo "== 1. 从镜像取原文件并核对 sha256（$IMAGE）"
while read -r rel want; do
  [ -n "$rel" ] || continue
  mkdir -p "$stage/$(dirname "$rel")"
  docker run --rm --entrypoint cat "$IMAGE" "/app/api/$rel" >"$stage/$rel"
  got="$(sha256sum "$stage/$rel" | cut -d' ' -f1)"
  if [ "$got" != "$want" ]; then
    echo "FAIL: $rel 与固定的上游版本不一致" >&2
    echo "  期望 $want" >&2
    echo "  实际 $got" >&2
    echo "  镜像换了。补丁必须按新版本重做，不能把陈旧结果挂上去。" >&2
    exit 1
  fi
  echo "ok: $rel"
done <<EOF
$PINNED
EOF

echo "== 2. 打补丁（--fuzz=0：上下文对不上就失败）"
for p in "$VERSION_DIR"/*.patch; do
  patch -p1 --fuzz=0 -s -d "$stage" <"$p"
  echo "ok: $(basename "$p")"
done

echo "== 3. 产出只读挂载树 $OUT"
# 目录结构与 /app/api 一一对应；compose 把**每个文件**单独只读挂进去（挂目录会把
# 整个 /app/api 遮掉）。目录本身留给 root 可写，文件 0444 给 api(uid 1001) 读。
rm -rf "$OUT"
mkdir -p "$OUT"
(cd "$stage" && tar -cf - .) | (cd "$OUT" && tar -xf -)
find "$OUT" -type f -exec chmod 0444 {} +
echo "ok: $(find "$OUT" -type f | wc -l) 个文件就位（0444）"
echo
echo "下一步：确认 docker-compose.yaml 里的文件挂载项与本目录补丁集一致"
echo "    （deploy/dify-36/check.sh 会断言此事），然后 docker compose up -d api worker"
