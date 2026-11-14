#!/usr/bin/env bash
# 开发机从已合并修订构建离线制品；服务宿主不需要源码checkout或访问PyPI。
set -euo pipefail
umask 027
[ "$#" = 2 ] || { echo '用法: build.sh <合并修订> <输出.tar.gz>' >&2; exit 2; }
revision=$(git rev-parse "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
out=$(realpath -m "$2")
[ ! -e "$out" ] || { echo '输出已存在' >&2; exit 2; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive "$revision" | tar -xf - -C "$work"
python3 - "$work" "$revision" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1]);p=root/'src/aiops_diagnostics/__init__.py'
s=p.read_text();version=(root/'VERSION').read_text().strip()
p.write_text(s.replace(f'__version__ = "{version}"', f'__version__ = "{version}+{sys.argv[2][:12]}"'))
PY
mkdir "$work/bundle" "$work/bundle/wheelhouse"
(cd "$work" && uv build --wheel --no-create-gitignore --out-dir "$work/bundle/wheelhouse")
(cd "$work" && uv export --frozen --no-dev --no-editable --no-emit-project --output-file "$work/requirements.txt")
python3 -m pip download --only-binary=:all: --python-version 313 \
  --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 \
  -r "$work/requirements.txt" --dest "$work/bundle/wheelhouse"
mkdir -p "$work/bundle/ops/environments"
cp -a "$work/ops/environments/env95" "$work/bundle/ops/environments/"
printf '%s\n' "$revision" > "$work/bundle/REVISION"
(cd "$work/bundle" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$work/SHA256SUMS")
mv "$work/SHA256SUMS" "$work/bundle/SHA256SUMS"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work/bundle" .
chmod 600 "$out"
sha256sum "$out"
