#!/usr/bin/env bash
# 仅在95服务宿主运行已合并制品；候选健康通过前旧实例继续服务。
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] || { echo '安装需要主机维护权限' >&2; exit 2; }
[ "$#" = 1 ] || { echo '用法: install.sh <已展开的完整制品目录>' >&2; exit 2; }
bundle=$(realpath -- "$1")
revision=$(cat "$bundle/REVISION")
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
python3 - "$bundle" <<'PYPREFLIGHT'
import hashlib,pathlib,sys
root=pathlib.Path(sys.argv[1])
expected={}
for line in (root/'SHA256SUMS').read_text().splitlines():
    digest,name=line.split('  ',1)
    p=pathlib.PurePosixPath(name)
    assert not p.is_absolute() and '..' not in p.parts and name not in expected
    expected[name.removeprefix('./')]=digest
actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p!=root/'SHA256SUMS'}
assert actual==set(expected), 'artifact file set mismatch'
assert {'REVISION','ops/environments/env95/aiops-gateway-95.service'} <= actual
assert all(name=='REVISION' or name.startswith('ops/environments/env95/') or name.startswith('wheelhouse/') for name in actual)
assert sum(name.startswith('wheelhouse/aiops_diagnostics-') and name.endswith('.whl') for name in actual)==1
assert all(not name.startswith('wheelhouse/') or name.endswith('.whl') for name in actual)
assert not any(p.is_symlink() for p in root.rglob('*')), 'artifact symlink refused'
for name,digest in expected.items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest, 'artifact digest mismatch'
for value in ['/opt/aiops-95','/var/lib/aiops-95','/var/backups/aiops-95','/etc/aiops-95','/home/aiops/AI-Ops','/home/aiops/.local/share/aiops-diagnostics']:
    p=pathlib.Path(value)
    assert not any(x.is_symlink() for x in [p,*p.parents]), 'installation path symlink refused'
PYPREFLIGHT
exec 9>/run/lock/aiops-95-install.lock
flock -n 9 || { echo '另一个安装正在执行' >&2; exit 2; }
[ -s /etc/aiops-95/gateway.env ] && [ -s /etc/aiops-95/server.env ] || {
  echo '先按接入手册准备本环境私有配置，不能复制另一环境身份' >&2; exit 2;
}
attempt=$(date -u +%Y%m%dT%H%M%SZ)-$$
release=/opt/aiops-95/releases/$revision-$attempt
backup=/var/backups/aiops-95/$revision/$attempt
[ ! -e "$release" ] && [ ! -e "$backup" ] || exit 2
[ ! -e /var/lib/aiops-95/installed-revision ] || { echo '首次重建已完成，不能覆盖已运行实例' >&2; exit 2; }
install -d -m 755 /opt/aiops-95 /opt/aiops-95/releases
install -d -m 700 /var/backups/aiops-95 "/var/backups/aiops-95/$revision" "$backup"
install -d -m 700 -o aiops -g aiops /var/lib/aiops-95
install -d -m 755 "$release"
cp -a "$bundle/." "$release/"
python3 -m venv "$release/venv"
"$release/venv/bin/pip" install --no-index --find-links "$release/wheelhouse" "$release"/wheelhouse/aiops_diagnostics-*.whl > "$backup/pip.log" 2>&1
chmod -R a+rX "$release"
# 仅归档已确认属于旧AI-Ops的路径，保留绝对路径和元数据供恢复。
tar -cpf "$backup/legacy.tar" --absolute-names \
  /home/aiops/AI-Ops /home/aiops/gateway.env \
  /home/aiops/.config/aiops-diagnostics /home/aiops/.local/share/aiops-diagnostics \
  /etc/systemd/system/aiops-gateway.service
sha256sum "$backup/legacy.tar" > "$backup/legacy.tar.sha256"
tar -tf "$backup/legacy.tar" > "$backup/legacy-files.txt"
python3 - "$backup/gateway-consistent.db" <<'PYSNAPSHOT'
import sqlite3,sys
with sqlite3.connect('file:/home/aiops/.local/share/aiops-diagnostics/gateway/gateway.db?mode=ro',uri=True) as source:
    with sqlite3.connect(sys.argv[1]) as target:
        source.backup(target)
        assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
PYSNAPSHOT
cp -a /etc/aiops-95 "$backup/new-config"
old_current=$(readlink /opt/aiops-95/current || true)
printf '%s\n' "$old_current" > "$backup/previous-current"
# 新配置用独立状态根；临时动态端口只做候选健康检查，不改变现役反代。
port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
unit=aiops95-verify-${revision:0:12}
verified=0
cleanup() { systemctl stop "$unit" >/dev/null 2>&1 || true; }
trap cleanup EXIT
systemd-run --unit "$unit" --property=User=aiops --property=Group=aiops \
  --property=WorkingDirectory="$release" --property=EnvironmentFile=/etc/aiops-95/gateway.env \
  --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
  "$release/venv/bin/aiops-gateway" serve --port "$port" >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" > "$backup/candidate-health.json"; then verified=1; break; fi
  sleep 1
done
[ "$verified" = 1 ] || { echo '候选健康失败，旧服务未停止' >&2; exit 1; }
python3 - "$backup/candidate-health.json" "${revision:0:12}" <<'PY'
import json,sys
body=json.load(open(sys.argv[1]))
assert body['ok'] and body['business_mutations']=='disabled'
assert body['version'].endswith('+'+sys.argv[2])
PY
cleanup
# 切换与旧资产退休分开；失败自动恢复旧服务，保留新实例现场。
cutover=0
rollback() {
  local rc=$?
  cleanup
  if [ "$rc" != 0 ] && [ "$cutover" = 1 ]; then
    systemctl stop aiops-gateway-95.service >/dev/null 2>&1 || true
    if [ -n "$old_current" ]; then
      ln -s "$old_current" /opt/aiops-95/current.rollback
      mv -Tf /opt/aiops-95/current.rollback /opt/aiops-95/current
    else
      rm -f /opt/aiops-95/current
    fi
    rm -f /var/lib/aiops-95/installed-revision /var/lib/aiops-95/installed-backup
    systemctl disable aiops-gateway-95.service >/dev/null 2>&1 || true
    systemctl start aiops-gateway.service
    echo '切换失败，旧服务已恢复；保留新现场' >&2
  fi
  exit "$rc"
}
trap rollback EXIT
cutover=1
systemctl stop aiops-gateway.service
# 最后一次一致快照在旧服务停止后，包含候选预检期间可能完成的写入。
python3 - "$backup/gateway-consistent.db" <<'PYFINALDB'
import sqlite3,sys
with sqlite3.connect('file:/home/aiops/.local/share/aiops-diagnostics/gateway/gateway.db?mode=ro',uri=True) as source:
    with sqlite3.connect(sys.argv[1]) as target:
        source.backup(target)
        assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
PYFINALDB
ln -s "$release" /opt/aiops-95/current.new
mv -Tf /opt/aiops-95/current.new /opt/aiops-95/current
install -m 644 "$bundle/ops/environments/env95/aiops-gateway-95.service" /etc/systemd/system/
systemctl daemon-reload
systemctl start aiops-gateway-95.service
verified=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 http://127.0.0.1:8788/health > "$backup/installed-health.json"; then verified=1; break; fi
  sleep 1
done
if [ "$verified" != 1 ]; then
  systemctl stop aiops-gateway-95.service || true
  systemctl start aiops-gateway.service
  echo '新服务健康失败，已恢复旧服务；新现场保留' >&2
  exit 1
fi
python3 - "$backup/installed-health.json" "${revision:0:12}" <<'PYVERIFY'
import json,sys
body=json.load(open(sys.argv[1]))
assert body['ok'] and body['business_mutations']=='disabled'
assert body['version'].endswith('+'+sys.argv[2])
PYVERIFY
systemctl enable aiops-gateway-95.service
printf '%s\n' "$backup" > /var/lib/aiops-95/installed-backup
printf '%s\n' "$revision" > /var/lib/aiops-95/installed-revision
cutover=0
printf '%s\n' '新服务已切换。先完成真实验收，再执行retire.sh清理旧路径。'
