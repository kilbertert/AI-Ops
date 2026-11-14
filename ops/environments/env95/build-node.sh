#!/usr/bin/env bash
# 将合并部署资产与锁定JAR打成完整节点包；不带配置或凭据。
set -euo pipefail
umask 077
[ "$#" = 4 ] || { echo '用法: build-node.sh REV NODE.jar PLATFORM.jar OUT.tar.gz' >&2; exit 2; }
revision=$(git rev-parse "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m "$4")
[ ! -e "$out" ] || exit 2
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive "$revision" deploy/node-runtime deploy/m2-live-03 ops/environments/env95 services/model-proxy tools/content/render_node_callback.py src/aiops_diagnostics/__init__.py src/aiops_diagnostics/operator/__init__.py src/aiops_diagnostics/operator/operator_tools.py src/aiops_diagnostics/operator/operator_tools.json | tar -xf - -C "$work"
python3 - "$work" "$2" "$3" <<'PY'
import hashlib,json,pathlib,shutil,sys
root=pathlib.Path(sys.argv[1]);lock=json.loads((root/'deploy/m2-live-03/images.lock.json').read_text())
for key,source in zip(['node','platform'],sys.argv[2:]):
    source=pathlib.Path(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==lock[key]['sha256'], f'{key} artifact mismatch'
    (root/key).mkdir()
    shutil.copyfile(source,root/key/lock[key]['artifact'])
PY
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$work.manifest")
mv "$work.manifest" "$work/SHA256SUMS"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
