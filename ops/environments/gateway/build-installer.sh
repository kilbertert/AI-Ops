#!/usr/bin/env bash
# 只从合并修订打包固定安装器；应用wheel与配置另按摘要传入。
set -euo pipefail
umask 077
[[ $# == 2 ]] || { echo 'usage: build-installer.sh MERGED_REV OUT.tar.gz'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m -- "$2")
[[ ! -e $out ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
for name in install.py runtime_check.py probe.py; do
  git show "$revision:ops/environments/gateway/$name" > "$work/$name"
done
git show "$revision:src/aiops_diagnostics/gateway/environment_artifacts.py" > "$work/artifact_checks.py"
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && sha256sum REVISION install.py runtime_check.py probe.py artifact_checks.py > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
