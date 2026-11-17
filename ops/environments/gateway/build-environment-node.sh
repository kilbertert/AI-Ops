#!/usr/bin/env bash
# 共享环境节点只带锁定节点JAR与部署资产，不携平台/数据库/凭据。
set -euo pipefail
umask 077
[[ $# = 3 ]] || { echo 'usage: build-environment-node.sh MERGED_REV NODE.jar OUT.tar.gz'; exit 2; }
revision=$(git rev-parse --verify "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m "$3")
[[ ! -e $out ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
git archive "$revision" deploy/node-runtime/environment.compose.yaml deploy/node-runtime/environment.md \
  deploy/node-runtime/aiops-environment-node@.service \
  deploy/node-runtime/gateway-callback-locations.conf.template \
  deploy/node-runtime/gateway-callback.conf.template deploy/m2-live-03/images.lock.json \
  tools/content/render_node_callback.py src/aiops_diagnostics/__init__.py \
  src/aiops_diagnostics/operator/__init__.py src/aiops_diagnostics/operator/operator_tools.py \
  src/aiops_diagnostics/operator/operator_tools.json | tar -xf - -C "$work"
python3 - "$work" "$2" <<'PY'
import hashlib,json,pathlib,shutil,sys
root=pathlib.Path(sys.argv[1]);source=pathlib.Path(sys.argv[2])
node=json.loads((root/'deploy/m2-live-03/images.lock.json').read_text())['node']
if hashlib.sha256(source.read_bytes()).hexdigest()!=node['sha256']:
    raise SystemExit('node artifact mismatch')
(root/'node').mkdir()
shutil.copyfile(source,root/'node'/node['artifact'])
PY
printf '%s\n' "$revision" > "$work/REVISION"
(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$work.manifest")
mv "$work.manifest" "$work/SHA256SUMS"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work" .
sha256sum "$out"
