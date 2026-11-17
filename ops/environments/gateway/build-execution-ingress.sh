#!/usr/bin/env bash
# 从合并修订生成共享平台入口；实际主机/来源/CA位置仅作为私有部署参数。
set -euo pipefail
umask 077
[[ $# -ge 6 ]] || { echo 'usage: build-execution-ingress.sh MERGED_REV OUT.tar.gz UPSTREAM TLS_NAME CA_FILE SOURCE_IP...'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
upstream=$3
tls_name=$4
ca_file=$5
shift 5
[[ ! -e $out ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
git archive "$revision" tools/content/render_execution_ingress.py | tar -xf - -C "$work"
mkdir "$work/bundle"
args=()
for source in "$@"; do args+=(--allow-source "$source"); done
python3 "$work/tools/content/render_execution_ingress.py" --upstream "$upstream" \
  --server-name "$tls_name" --ca-file "$ca_file" "${args[@]}" \
  --out "$work/bundle/aiops-execution-ingress.conf"
printf '%s\n' "$revision" > "$work/bundle/REVISION"
(cd "$work/bundle" && sha256sum REVISION aiops-execution-ingress.conf > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work/bundle" .
sha256sum "$out"
