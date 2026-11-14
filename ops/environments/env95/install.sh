#!/usr/bin/env bash
# 仅在95服务宿主运行已合并制品；候选健康通过前旧实例继续服务。
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] || { echo '安装需要主机维护权限' >&2; exit 2; }
[ "$#" = 1 ] || { echo '用法: install.sh <已展开的完整制品目录>' >&2; exit 2; }
bundle=$(realpath -- "$1")
revision=$(cat "$bundle/REVISION")
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
(cd "$bundle" && sha256sum --check SHA256SUMS >/dev/null)
[ -s /etc/aiops-95/gateway.env ] && [ -s /etc/aiops-95/server.env ] || {
  echo '先按接入手册准备本环境私有配置，不能复制另一环境身份' >&2; exit 2;
}
release=/opt/aiops-95/releases/$revision
backup=/var/backups/aiops-95/$revision
[ ! -e "$release" ] && [ ! -e "$backup" ] || { echo '本修订已执行，先核对现状' >&2; exit 2; }
install -d -m 755 /opt/aiops-95 /opt/aiops-95/releases
install -d -m 700 /var/backups/aiops-95 "$backup"
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
    systemctl start aiops-gateway.service
    echo '切换失败，旧服务已恢复；保留新现场' >&2
  fi
  exit "$rc"
}
trap rollback EXIT
cutover=1
systemctl stop aiops-gateway.service
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
printf '%s\n' "$revision" > /var/lib/aiops-95/installed-revision
cutover=0
printf '%s\n' '新服务已切换。先完成真实验收，再执行retire.sh清理旧路径。'
