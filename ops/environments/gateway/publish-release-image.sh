#!/usr/bin/env bash
# 官方单一 Gateway OCI 镜像构建、冒烟核验、推送阿里云 ACR 与 Release 清单挂载入口（ADR-0363）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  publish-release-image.sh --tag <vX.Y.Z> [--allow-skip-unconfigured]

环境变量:
  AIOPS_GATEWAY_IMAGE_REPO   目标镜像仓库（如 qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway）
  AIOPS_ACR_USERNAME         ACR 推送用户名
  AIOPS_ACR_PASSWORD         ACR 推送密码（永不进 argv）
  GH_TOKEN                   GitHub CLI 令牌（用于挂载 gateway-image.json 至 Release）
EOF
  exit 2
}

TAG=""
ALLOW_SKIP=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || usage
      TAG="$2"
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

[ -n "$TAG" ] || usage
[[ "$TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
  echo "目标标签不是合法 SemVer vX.Y.Z: $TAG" >&2
  exit 2
}

IMAGE_REPO="${AIOPS_GATEWAY_IMAGE_REPO:-}"
ACR_USER="${AIOPS_ACR_USERNAME:-}"
ACR_PASS="${AIOPS_ACR_PASSWORD:-}"

if [ -z "$IMAGE_REPO" ] || [ -z "$ACR_USER" ] || [ -z "$ACR_PASS" ]; then
  msg="未配置 AIOPS_GATEWAY_IMAGE_REPO / AIOPS_ACR_USERNAME / AIOPS_ACR_PASSWORD，跳过 OCI 镜像推送。"
  echo "$msg"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### 客户机 Gateway OCI 镜像构建"
      echo "- 状态：未配置阿里云 ACR 变量，已跳过推送（不阻塞发布）"
      echo "- 所需配置：\`AIOPS_GATEWAY_IMAGE_REPO\` 与 \`AIOPS_ACR_USERNAME\` / \`AIOPS_ACR_PASSWORD\`"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  [ "$ALLOW_SKIP" = "1" ] && exit 0
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
cd "$REPO_ROOT"

git fetch --prune origin main
git fetch origin "refs/tags/${TAG}:refs/tags/${TAG}"
[ "$(git cat-file -t "$TAG")" = "tag" ] || {
  echo "标签 $TAG 不是注释标签（annotated tag），拒绝发布" >&2
  exit 1
}
rev=$(git rev-parse --verify "${TAG}^{commit}")
git merge-base --is-ancestor "$rev" origin/main || {
  echo "标签 $TAG ($rev) 不是 origin/main 的祖先，拒绝发布" >&2
  exit 1
}

registry_host="${IMAGE_REPO%%/*}"
docker_cfg=$(mktemp -d)
meta_dir=$(mktemp -d)
meta_file="${meta_dir}/gateway-image.json"
trap 'rm -rf "$docker_cfg" "$meta_dir"' EXIT
export DOCKER_CONFIG="$docker_cfg"

printf '%s' "$ACR_PASS" | docker login "$registry_host" --username "$ACR_USER" --password-stdin

bash "$SCRIPT_DIR/build-image.sh" \
  --rev "$TAG" \
  --repo "$IMAGE_REPO" \
  --tag "$TAG" \
  --push \
  --output-metadata "$meta_file"

if ! gh release view "$TAG" >/dev/null 2>&1; then
  gh release create "$TAG" --verify-tag \
    --title "AI-Ops $TAG" \
    --notes "客户机 Gateway OCI 容器镜像已构建发布。"
fi
gh release upload "$TAG" "$meta_file" --clobber

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  read -r full_version image_ref ingress_sha < <(
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("full_version",""), d.get("image_ref",""), d.get("ingress_sha256",""))' "$meta_file"
  )
  {
    echo "### 客户机 Gateway OCI 镜像构建与发布"
    echo "- 官方标签：\`${TAG}\`（\`${full_version}\`）"
    echo "- 不可变镜像摘要：\`${image_ref}\`"
    echo "- 受控 Nginx 入口模板哈希：\`${ingress_sha}\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi
