#!/usr/bin/env bash
# 只从已合并修订打包；不携带证书或来源地址。
set -euo pipefail
umask 077
[[ $# == 2 ]] || { echo 'usage: build-service-api-bundle.sh REV OUT'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
[[ ! -e $out ]] || exit 1
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
for name in dify-service-api-TLS.conf service-api-deploy.sh; do
    git show "$revision:ops/dify-console-bridge/$name" > "$work/$name"
done
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && sha256sum REVISION dify-service-api-TLS.conf service-api-deploy.sh > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
