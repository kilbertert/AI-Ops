#!/usr/bin/env bash
# 固定合并修订生成环境用户助手入口片段；环境号为部署参数，凭据不入制品。
set -euo pipefail
umask 077
[[ $# -eq 3 ]] || { echo 'usage: build-user-ingress.sh MERGED_REV OUT.tar.gz ENV_ID'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
env_id=$3
[[ $env_id =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || { echo 'ENV_ID 只允许小写字母数字与连字符'; exit 2; }
[[ ! -e $out ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
git archive "$revision" \
  ops/nginx/environment-user-ingress.conf \
  ops/nginx/environment-user-ingress-proxy.inc \
  ops/nginx/environment-user-ingress-routes.txt \
  | tar -xf - -C "$work"
mkdir "$work/bundle"
for name in environment-user-ingress.conf environment-user-ingress-proxy.inc \
  environment-user-ingress-routes.txt; do
  sed "s/__ENV__/${env_id}/g" "$work/ops/nginx/$name" > "$work/bundle/$name"
done
grep -q '__ENV__' "$work/bundle/environment-user-ingress.conf" && exit 2
printf '%s\n' "$revision" > "$work/bundle/REVISION"
(cd "$work/bundle" && sha256sum \
  REVISION environment-user-ingress.conf environment-user-ingress-proxy.inc \
  environment-user-ingress-routes.txt > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work/bundle" .
sha256sum "$out"
