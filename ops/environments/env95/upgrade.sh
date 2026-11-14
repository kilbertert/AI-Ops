#!/usr/bin/env bash
# 95同schema升级：保留配置/数据/首次重建恢复点，失败切回原制品。
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] && [ "$#" = 1 ] || exit 2
bundle=$(realpath -- "$1")
bash "$(dirname "$0")/install.sh" "$bundle" --verify-only
revision=$(cat "$bundle/REVISION")
exec 9>/run/lock/aiops-95-install.lock
flock -n 9 || { echo '另一个安装正在执行' >&2; exit 2; }
[ -s /var/lib/aiops-95/installed-revision ] || { echo '先完成首次重建' >&2; exit 2; }
old_current=$(readlink -f /opt/aiops-95/current)
[[ "$old_current" == /opt/aiops-95/releases/* ]] && [ -s "$old_current/REVISION" ] || exit 2
old_revision=$(cat "$old_current/REVISION")
[[ "$old_revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
python3 - <<'PY'
from pathlib import Path
for value in ['/opt/aiops-95/releases','/var/backups/aiops-95/upgrades','/var/lib/aiops-95/installed-revision']:
    path=Path(value)
    assert not any(p.is_symlink() for p in [path,*path.parents]), 'upgrade path symlink refused'
PY
systemctl is-active --quiet aiops-gateway-95
attempt=$(date -u +%Y%m%dT%H%M%SZ)-$$
release=/opt/aiops-95/releases/$revision-$attempt
backup=/var/backups/aiops-95/upgrades/$attempt
[ ! -e "$release" ] && [ ! -e "$backup" ] || exit 2
install -d -m 700 "$backup"
install -d -m 755 "$release"
cp -a "$bundle/." "$release/"
python3 -m venv "$release/venv"
"$release/venv/bin/pip" install --no-index --find-links "$release/wheelhouse" "$release"/wheelhouse/aiops_diagnostics-*.whl > "$backup/pip.log" 2>&1
chmod -R a+rX "$release"
# 升级不变更服务单元；不相容的服务/数据库迁移必须有单独变更流程。
cmp "$bundle/ops/environments/env95/aiops-gateway-95.service" /etc/systemd/system/aiops-gateway-95.service
check_state() {
  runuser -u aiops -- "$release/venv/bin/python" - <<'PY'
import os, sqlite3
from pathlib import Path
from dotenv import dotenv_values
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import JOB_TABLES
os.environ.update({k:v for k,v in dotenv_values('/etc/aiops-95/gateway.env',interpolate=False).items() if v is not None})
s=GatewayServerSettings.from_env()
assert s.environment=='95' and s.database_file==Path('/var/lib/aiops-95/gateway/gateway.db')
assert not s.chat_route_bindings_file or Path(s.chat_route_bindings_file)==s.database_file
report=gateway_schema.check(s.database_file)
assert report['version']==report['current_version'], 'schema migration requires separate plan'
with sqlite3.connect(f'file:{s.database_file}?mode=ro',uri=True) as db:
    checks=[(table,'status',('queued','running')) for table in JOB_TABLES]
    checks.extend([('agent_runs','state',('issued','running')),('operator_setup_jobs','state',('pending','running'))])
    for table,column,states in checks:
        marks=','.join('?' for _ in states)
        assert db.execute(f'SELECT count(*) FROM {table} WHERE {column} IN ({marks})',states).fetchone()[0]==0, f'inflight {table}'
print('same-schema and zero-inflight passed')
PY
}
check_state > "$backup/preflight.txt"
cp -a /etc/aiops-95 "$backup/config"
printf '%s\n' "$old_current" > "$backup/previous-current"
stopped=0
rollback() {
  rc=$?
  if [ "$rc" != 0 ] && [ "$stopped" = 1 ]; then
    systemctl stop aiops-gateway-95.service || true
    ln -s "$old_current" "/opt/aiops-95/current.rollback-$attempt"
    mv -Tf "/opt/aiops-95/current.rollback-$attempt" /opt/aiops-95/current
    rm -f "/opt/aiops-95/current.new-$attempt"
    systemctl start aiops-gateway-95.service
    echo '升级失败，已启动原制品；需复验原版本健康，数据和失败现场保留' >&2
  fi
  exit "$rc"
}
trap rollback EXIT
stopped=1
systemctl stop aiops-gateway-95.service
# 停稳后二次检查，覆盖第一次检查到停止之间的新请求；在飞记录保留，不能重置它。
check_state > "$backup/stopped-preflight.txt"
tar -cpf "$backup/state.tar" -C /var/lib aiops-95
sha256sum "$backup/state.tar" > "$backup/state.tar.sha256"
ln -s "$release" "/opt/aiops-95/current.new-$attempt"
mv -Tf "/opt/aiops-95/current.new-$attempt" /opt/aiops-95/current
systemctl start aiops-gateway-95.service
verified=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8788/health > "$backup/health.json"; then verified=1; break; fi
  sleep 1
done
[ "$verified" = 1 ]
python3 - "$backup/health.json" "${revision:0:12}" <<'PY'
import json,sys
body=json.load(open(sys.argv[1]))
assert body['ok'] and body['business_mutations']=='disabled'
assert body['version'].endswith('+'+sys.argv[2])
PY
printf '%s\n' "$revision" > "$backup/deployed-revision"
stopped=0
printf '%s\n' "升级健康通过，真实业务待另验；恢复资料: $backup"
