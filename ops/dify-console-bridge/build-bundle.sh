#!/usr/bin/env bash
# 在开发机从一个确定的 Git 修订打包；部署时必须选择已合并的 origin/main。
set -euo pipefail
umask 027
[ "$#" -eq 2 ] || { echo 'usage: build-bundle.sh REV OUT.tar.gz' >&2; exit 2; }
repo=$(git rev-parse --show-toplevel)
revision=$(git rev-parse --verify "$1^{commit}")
out=$(realpath -m -- "$2")
[ ! -e "$out" ] || { echo '输出已存在' >&2; exit 2; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$repo" archive "$revision" \
  src/aiops_diagnostics/{__init__,dify_console_publish,async_job_lifecycle,bounded_http,http_auth}.py \
  ops/dify-console-bridge/{dify-publish-button.js,operator-native-preview.js,dify-console-publish.conf,deploy.sh} \
  | tar -xf - -C "$work"
mkdir "$work/bundle"
mv "$work/src/aiops_diagnostics" "$work/bundle/"
mv "$work/ops/dify-console-bridge/"* "$work/bundle/"
cat "$work/bundle/operator-native-preview.js" >> "$work/bundle/dify-publish-button.js"
printf '%s\n' "$revision" > "$work/bundle/REVISION"
(
  cd "$work/bundle"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$work/SHA256SUMS"
)
mv "$work/SHA256SUMS" "$work/bundle/"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  -czf "$out" -C "$work/bundle" .
chmod 600 "$out"
sha256sum "$out"
