#!/usr/bin/env bash
# 固定合并修订生成单一环境回调片段；来源IP为私有部署参数，不入仓。
set -euo pipefail
umask 077
[[ $# -ge 4 ]] || { echo 'usage: build-ingress.sh MERGED_REV OUT.tar.gz GATEWAY SOURCE_IP...'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
gateway=$3
shift 3
[[ ! -e $out ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
git archive "$revision" tools/content/render_node_callback.py deploy/node-runtime \
  ops/nginx/dify-consumer-callback.conf src | tar -xf - -C "$work"
mkdir "$work/bundle"
args=()
for source in "$@"; do args+=(--allow-source "$source"); done
PYTHONPATH="$work/src" python3 "$work/tools/content/render_node_callback.py" \
  --ingress --gateway "$gateway" "${args[@]}" --out "$work/bundle/aiops-tool-ingress.conf"
printf '%s\n' "$revision" > "$work/bundle/REVISION"
(cd "$work/bundle" && sha256sum REVISION aiops-tool-ingress.conf > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work/bundle" .
sha256sum "$out"
