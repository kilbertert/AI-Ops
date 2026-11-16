#!/usr/bin/env bash
# 从合并修订构建指定Python小版本的Linux x86_64离线预演制品。
set -euo pipefail
umask 027
[ "$#" = 3 ] || { echo 'usage: build.sh MERGED_REV PYTHON_MINOR OUT.tar.gz' >&2; exit 2; }
revision=$(git rev-parse "$1^{commit}")
git merge-base --is-ancestor "$revision" origin/main
minor=$2
[[ "$minor" =~ ^3\.(11|12|13)$ ]] || { echo 'Python版本仅支持3.11/3.12/3.13' >&2; exit 2; }
out=$(realpath -m "$3")
[ ! -e "$out" ] || { echo '输出已存在' >&2; exit 2; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive "$revision" pyproject.toml uv.lock README.md VERSION src | tar -xf - -C "$work"
python3 - "$work" "$revision" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1]);p=root/'src/aiops_diagnostics/__init__.py'
version=(root/'VERSION').read_text().strip();old=f'__version__ = "{version}"'
text=p.read_text();assert text.count(old)==1
p.write_text(text.replace(old,f'__version__ = "{version}+{sys.argv[2][:12]}"'))
PY
mkdir -p "$work/bundle/wheelhouse"
(cd "$work" && uv build --wheel --no-create-gitignore --out-dir "$work/bundle/wheelhouse")
(cd "$work" && uv export --frozen --no-dev --no-editable --no-emit-project --output-file "$work/requirements.txt")
# --python-version不改变PEP508 marker求值的宿主，故用同小版本解释器运行pip。
uv venv --python "$minor" --seed "$work/build-python" >/dev/null
"$work/build-python/bin/python" -m pip download --only-binary=:all: \
  --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 \
  --python-version "$minor" -r "$work/requirements.txt" -d "$work/bundle/wheelhouse"
python3 - "$work" "$revision" "$minor" <<'PY'
import hashlib,json,sys
from pathlib import Path
work=Path(sys.argv[1]);root=work/'bundle'
meta={'schema':1,'revision':sys.argv[2],'python':sys.argv[3],'platform':'linux-x86_64',
      'version':(work/'VERSION').read_text().strip()+'+'+sys.argv[2][:12]}
(root/'manifest.json').write_text(json.dumps(meta)+'\n')
(root/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(root))+'\n' for p in sorted(root.rglob('*')) if p.is_file()))
PY
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out" -C "$work/bundle" manifest.json SHA256SUMS wheelhouse
chmod 600 "$out"
sha256sum "$out"
