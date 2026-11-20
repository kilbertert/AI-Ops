#!/usr/bin/env bash
# 从合并修订打包现役转发器；不带任何连接凭据。
set -euo pipefail
umask 077
[[ $# == 2 ]] || { echo 'usage: build-bundle.sh MERGED_REV OUT.tar.gz'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
[[ ! -e $out ]] || exit 1
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
for name in aiops-inner-api-forward.py deploy.sh; do
    git show "$revision:ops/dify-inner-api/$name" > "$work/$name"
done
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && sha256sum REVISION aiops-inner-api-forward.py deploy.sh > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
