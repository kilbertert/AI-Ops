#!/usr/bin/env bash
# 从已合并至 origin/main 的修订构建、验证并可选推送客户机 AI-Ops Gateway OCI 镜像（ADR-0363）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  build-image.sh --rev <REV_OR_TAG> --repo <IMAGE_REPO> [--tag <IMAGE_TAG>] \
                 [--push] [--output-metadata <PATH>] [--base-image <IMG>]
EOF
  exit 2
}

REV=""
REPO=""
IMAGE_TAG=""
PUSH=0
OUTPUT_META=""
BASE_IMAGE="${AIOPS_GATEWAY_BASE_IMAGE:-python:3.12-slim}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --rev)
      [ "$#" -ge 2 ] || usage
      REV="$2"
      shift 2
      ;;
    --repo)
      [ "$#" -ge 2 ] || usage
      REPO="$2"
      shift 2
      ;;
    --tag)
      [ "$#" -ge 2 ] || usage
      IMAGE_TAG="$2"
      shift 2
      ;;
    --push)
      PUSH=1
      shift
      ;;
    --output-metadata)
      [ "$#" -ge 2 ] || usage
      OUTPUT_META="$2"
      shift 2
      ;;
    --base-image)
      [ "$#" -ge 2 ] || usage
      BASE_IMAGE="$2"
      shift 2
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

[ -n "$REV" ] && [ -n "$REPO" ] || usage
[[ "$REPO" =~ ^[a-z0-9][a-z0-9._:/-]{2,200}$ ]] || {
  echo "非法镜像仓库地址: $REPO" >&2
  exit 2
}
[[ "$BASE_IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:@/-]{1,200}$ ]] || {
  echo "非法基础镜像地址: $BASE_IMAGE" >&2
  exit 2
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)
DOCKERFILE="$SCRIPT_DIR/Dockerfile"
[ -f "$DOCKERFILE" ] || { echo "缺少 $DOCKERFILE" >&2; exit 2; }

cd "$REPO_ROOT"

revision=$(git rev-parse "${REV}^{commit}")
git merge-base --is-ancestor "$revision" origin/main || {
  echo "修订 $REV ($revision) 不是 origin/main 的祖先，拒绝构建正式镜像" >&2
  exit 1
}

short_sha=${revision:0:12}
version=$(git show "$revision:VERSION" | tr -d '[:space:]')
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
  echo "VERSION 不是合法 SemVer: $version" >&2
  exit 1
}
full_version="${version}+${short_sha}"

if [ -z "$IMAGE_TAG" ]; then
  if git rev-parse -q --verify "refs/tags/${REV}" >/dev/null 2>&1 && [ "$(git cat-file -t "$REV")" = "tag" ]; then
    IMAGE_TAG="$REV"
  else
    IMAGE_TAG="v${version}"
  fi
fi
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "非法镜像标签: $IMAGE_TAG" >&2
  exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
ctx="$work/ctx"
mkdir -p "$ctx/wheelhouse"

git archive "$revision" pyproject.toml uv.lock README.md VERSION src ops/nginx | tar -xf - -C "$work"

python3 - "$work" "$short_sha" "$version" <<'PY'
import hashlib
import sys
from pathlib import Path

work = Path(sys.argv[1])
short_sha = sys.argv[2]
version = sys.argv[3]

init_py = work / "src/aiops_diagnostics/__init__.py"
old = f'__version__ = "{version}"'
text = init_py.read_text(encoding="utf-8")
if text.count(old) != 1:
    raise SystemExit(f"注入版本号失败：未唯一匹配 {old}")
init_py.write_text(text.replace(old, f'__version__ = "{version}+{short_sha}"'), encoding="utf-8")

conf = (work / "ops/nginx/environment-user-ingress.conf").read_text(encoding="utf-8")
inc = (work / "ops/nginx/environment-user-ingress-proxy.inc").read_text(encoding="utf-8")
combined = conf.rstrip() + "\n---\n" + inc.rstrip() + "\n"
(work / "ingress_sha256.txt").write_text(
    hashlib.sha256(combined.encode("utf-8")).hexdigest(), encoding="utf-8"
)
PY

ingress_sha256=$(cat "$work/ingress_sha256.txt")

(cd "$work" && uv build --wheel --no-create-gitignore --out-dir "$ctx/wheelhouse" >/dev/null)
# 镜像使用 requirements.txt 作为锁定依赖底座（带 --hash），不修改原仓库 uv.lock。
(cd "$work" && uv export --frozen --no-dev --no-editable --no-emit-project --output-file "$ctx/requirements.txt" >/dev/null)

primary_ref="${REPO}:${IMAGE_TAG}"
sha_tag_ref="${REPO}:sha-${short_sha}"

# 始终先本地构建 linux/amd64 并执行强制只读非 root 容器冒烟核验，通过后再推送到镜像仓库。
build_cmd=(docker buildx)
if ! docker buildx version >/dev/null 2>&1; then
  build_cmd=(docker)
fi

"${build_cmd[@]}" build \
  --platform linux/amd64 \
  --file "$DOCKERFILE" \
  --build-arg "PYTHON_BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "AIOPS_VERSION=${version}" \
  --build-arg "AIOPS_REVISION=${revision}" \
  --build-arg "AIOPS_FULL_VERSION=${full_version}" \
  --build-arg "AIOPS_INGRESS_SHA256=${ingress_sha256}" \
  --tag "$primary_ref" \
  --tag "$sha_tag_ref" \
  --iidfile "$work/iid.txt" \
  --load \
  "$ctx"

image_id=$(tr -d '[:space:]' < "$work/iid.txt")

# 推送前强制只读非 root 冒烟校验：确认入口与精确 __version__ 在隔离容器内可执行
smoked_ver=$(docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --user 65534:65534 \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --entrypoint /opt/aiops/venv/bin/python \
  "$primary_ref" \
  -c 'import aiops_diagnostics; print(aiops_diagnostics.__version__)')

if [ "$smoked_ver" != "$full_version" ]; then
  echo "容器冒烟版本不匹配：期望 $full_version，实际 $smoked_ver" >&2
  exit 1
fi

docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --user 65534:65534 \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  "$primary_ref" --help >/dev/null

image_digest=""
image_ref=""
if [ "$PUSH" = "1" ]; then
  docker push "$primary_ref"
  docker push "$sha_tag_ref"

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
  image_ref="${REPO}@${image_digest}"
fi

meta_json=$(python3 - "$REV" "$version" "$revision" "$short_sha" "$full_version" \
  "$REPO" "$IMAGE_TAG" "$image_digest" "$image_ref" "$image_id" "$ingress_sha256" <<'PY'
import json
import sys
from datetime import datetime, timezone

tag, version, rev, short_sha, full_ver, repo, img_tag, digest, img_ref, img_id, ngx_sha = sys.argv[1:12]
payload = {
    "schema": "aiops-gateway-image/1",
    "tag": tag if tag.startswith("v") else f"v{version}",
    "version": version,
    "revision": rev,
    "short_sha": short_sha,
    "full_version": full_ver,
    "image_repo": repo,
    "image_tag": img_tag,
    "image_digest": digest,
    "image_ref": img_ref,
    "image_id": img_id,
    "platform": "linux/amd64",
    "ingress_sha256": ngx_sha,
    "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
)

if [ -n "$OUTPUT_META" ]; then
  mkdir -p "$(dirname "$OUTPUT_META")"
  printf '%s\n' "$meta_json" > "$OUTPUT_META"
  chmod 644 "$OUTPUT_META"
fi

printf '%s\n' "$meta_json"
