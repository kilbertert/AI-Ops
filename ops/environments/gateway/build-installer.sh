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
names=(install.py runtime_check.py probe.py)
if git cat-file -e "$revision:ops/environments/gateway/task.py" 2>/dev/null; then
  names+=(task.py)
fi
if git cat-file -e "$revision:ops/environments/gateway/publication.py" 2>/dev/null; then
  names+=(publication.py)
fi
for name in "${names[@]}"; do
  git show "$revision:ops/environments/gateway/$name" > "$work/$name"
done
if git cat-file -e "$revision:src/aiops_diagnostics/onboarding/artifacts.py" 2>/dev/null; then
  git show "$revision:src/aiops_diagnostics/onboarding/artifacts.py" > "$work/artifact_checks.py"
else
  git show "$revision:src/aiops_diagnostics/gateway/environment_artifacts.py" > "$work/artifact_checks.py"
fi
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && sha256sum REVISION "${names[@]}" artifact_checks.py > SHA256SUMS)
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
