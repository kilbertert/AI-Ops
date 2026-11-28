#!/usr/bin/env bash
# 官方 Dify Web OCI 镜像构建、冒烟核验、推送阿里云 ACR 与元数据生成入口（ADR-0365）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  publish-web-image.sh [--ref <git-ref>] [--tag <extra-tag>] [--output-metadata <path>] [--upload-release <vX.Y.Z>] [--allow-skip-unconfigured]

环境变量:
  AIOPS_DIFY_WEB_IMAGE_REPO  目标镜像仓库（如 qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web）
  AIOPS_ACR_USERNAME         ACR 推送用户名
  AIOPS_ACR_PASSWORD         ACR 推送密码（仅走 --password-stdin，永不进 argv）
  GH_TOKEN                   GitHub CLI 令牌（仅在指定 --upload-release 时需要）
EOF
  exit 2
}

REF="HEAD"
EXTRA_TAG=""
META_OUT="./dify-web-image.json"
UPLOAD_RELEASE=""
ALLOW_SKIP=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref)
      [ "$#" -ge 2 ] || usage
      REF="$2"
      shift 2
      ;;
    --tag)
      [ "$#" -ge 2 ] || usage
      EXTRA_TAG="$2"
      shift 2
      ;;
    --output-metadata)
      [ "$#" -ge 2 ] || usage
      META_OUT="$2"
      shift 2
      ;;
    --upload-release)
      [ "$#" -ge 2 ] || usage
      UPLOAD_RELEASE="$2"
      shift 2
      ;;
    --allow-skip-unconfigured)
      ALLOW_SKIP=1
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

if [ -n "$EXTRA_TAG" ] && ! [[ "$EXTRA_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "附加标签格式不合法: $EXTRA_TAG" >&2
  exit 2
fi

if [ -n "$UPLOAD_RELEASE" ] && ! [[ "$UPLOAD_RELEASE" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Release 标签不是合法 SemVer vX.Y.Z: $UPLOAD_RELEASE" >&2
  exit 2
fi

IMAGE_REPO="${AIOPS_DIFY_WEB_IMAGE_REPO:-}"
ACR_USER="${AIOPS_ACR_USERNAME:-}"
ACR_PASS="${AIOPS_ACR_PASSWORD:-}"

if [ -z "$IMAGE_REPO" ] || [ -z "$ACR_USER" ] || [ -z "$ACR_PASS" ]; then
  msg="未配置 AIOPS_DIFY_WEB_IMAGE_REPO / AIOPS_ACR_USERNAME / AIOPS_ACR_PASSWORD，跳过 Dify Web ACR 镜像推送。"
  echo "$msg"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### Dify Web OCI 镜像发布（ACR）"
      echo "- 状态：未配置阿里云 ACR 变量，已跳过推送"
      echo "- 所需配置：\`AIOPS_DIFY_WEB_IMAGE_REPO\` 与 \`AIOPS_ACR_USERNAME\` / \`AIOPS_ACR_PASSWORD\`"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  [ "$ALLOW_SKIP" = "1" ] && exit 0
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
cd "$REPO_ROOT"

if git remote get-url origin >/dev/null 2>&1; then
  git fetch --prune origin main
fi

rev=$(git rev-parse --verify "${REF}^{commit}") || {
  echo "无法解析目标提交: $REF" >&2
  exit 1
}
short_sha="${rev:0:12}"

if git rev-parse --verify origin/main >/dev/null 2>&1; then
  git merge-base --is-ancestor "$rev" origin/main || {
    echo "目标提交 $rev 不是 origin/main 的祖先，拒绝发布 Dify Web 镜像" >&2
    exit 1
  }
fi

current_head=$(git rev-parse HEAD)
if [ "$current_head" != "$rev" ]; then
  git checkout --detach "$rev"
fi

primary_tag="1.17.1-${short_sha}"
primary_ref="${IMAGE_REPO}:${primary_tag}"

registry_host="${IMAGE_REPO%%/*}"
docker_cfg=$(mktemp -d)
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/dify-web-build-XXXXXX")
trap 'rm -rf "$docker_cfg" "$work_dir"' EXIT
export DOCKER_CONFIG="$docker_cfg"

DIFY_WEB_PATCHED_IMAGE="$primary_ref" DIFY_WEB_WORK="$work_dir" bash "$SCRIPT_DIR/build-web-image.sh"

# 非联网冒烟校验：确认 standalone server.js、入口脚本、Liquid Glass 与 OCI 发版界面已打入产物
docker run --rm --network none --entrypoint sh "$primary_ref" -c \
  'test -f /app/targets/next/web/server.js && test -x /app/entrypoint.sh && grep -rq "aiops-liquid-glass" /app/targets/next && grep -rq "待构建镜像" /app/targets/next'

printf '%s' "$ACR_PASS" | docker login "$registry_host" --username "$ACR_USER" --password-stdin

docker push "$primary_ref"
if [ -n "$EXTRA_TAG" ] && [ "$EXTRA_TAG" != "$primary_tag" ]; then
  extra_ref="${IMAGE_REPO}:${EXTRA_TAG}"
  docker tag "$primary_ref" "$extra_ref"
  docker push "$extra_ref"
fi

image_digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$primary_ref" 2>/dev/null || true)
image_digest="${image_digest##*@}"
if ! [[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] && docker buildx version >/dev/null 2>&1; then
  image_digest=$(docker buildx imagetools inspect "$primary_ref" --raw \
    | sha256sum | awk '{print "sha256:" $1}')
fi
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "推送后未能解析不可变 @sha256 摘要，拒绝输出不完整元数据: $image_digest" >&2
  exit 1
}

image_id=$(docker image inspect --format '{{.Id}}' "$primary_ref")
image_ref="${IMAGE_REPO}@${image_digest}"

meta_json=$(python3 - "$rev" "$short_sha" "$primary_tag" "$EXTRA_TAG" \
  "$IMAGE_REPO" "$image_digest" "$image_ref" "$image_id" <<'PY'
import json
import sys
from datetime import datetime, timezone

rev, short_sha, img_tag, extra_tag, repo, digest, img_ref, img_id = sys.argv[1:9]
payload = {
    "schema": "aiops-dify-web-image/1",
    "dify_upstream_version": "1.17.1",
    "revision": rev,
    "short_sha": short_sha,
    "tag": extra_tag or img_tag,
    "image_repo": repo,
    "image_tag": img_tag,
    "image_digest": digest,
    "image_ref": img_ref,
    "image_id": img_id,
    "platform": "linux/amd64",
    "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
)

mkdir -p "$(dirname "$META_OUT")"
printf '%s\n' "$meta_json" > "$META_OUT"
chmod 644 "$META_OUT"

if [ -n "$UPLOAD_RELEASE" ]; then
  gh release upload "$UPLOAD_RELEASE" "$META_OUT" --clobber
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Dify Web OCI 镜像发布（ACR）"
    echo "- 源提交：\`${rev}\`（\`${short_sha}\`）"
    echo "- 不可变镜像摘要：\`${image_ref}\`"
    echo "- 36 一键部署命令：\`bash deploy/dify-36/web-patches/deploy-web-image.sh --image ${image_ref}\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi

printf '%s\n' "$meta_json"
