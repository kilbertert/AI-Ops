#!/usr/bin/env bash
# 只安装/撤回本次API入口；来源清单由受控配置提供，不接收任意nginx指令。
set -euo pipefail
umask 077
[[ $# == 4 ]] || { echo 'usage: service-api-deploy.sh install|restore BUNDLE BACKUP SOURCES'; exit 2; }
action=$1
bundle=$(realpath -- "$2")
backup=$(realpath -m -- "$3")
sources=$(realpath -- "$4")
nginx=/www/server/nginx/sbin/nginx
target=/www/server/panel/vhost/nginx/aiops-dify-service-api.conf
allowed=/etc/aiops-dify-api/allowed-sources.conf
exec 9>/run/lock/aiops-dify-service-api.lock
flock -n 9 || { echo '入口变更正在执行'; exit 1; }

restore_files() {
    for name in target allowed; do
        local path
        if [[ $name == target ]]; then path=$target; else path=$allowed; fi
        if [[ -f $backup/$name ]]; then cp -p -- "$backup/$name" "$path"; else rm -f -- "$path"; fi
    done
    "$nginx" -t && "$nginx" -s reload
}

if [[ $action == restore ]]; then
    [[ -f $backup/READY ]] || { echo '缺少完整备份'; exit 1; }
    restore_files
    echo restored
    exit
fi
[[ $action == install ]] || exit 2
[[ ! -e $backup ]] || { echo '备份目录必须全新'; exit 1; }
[[ ! -L $target && ! -L $allowed ]] || exit 1
(cd "$bundle" && sha256sum --strict -c SHA256SUMS)
[[ $(cat "$bundle/REVISION") =~ ^[0-9a-f]{40}$ ]] || exit 1
python3 - "$sources" <<'PY'
import ipaddress, os, stat, sys
from pathlib import Path
p = Path(sys.argv[1]); s = p.lstat()
assert stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and not s.st_mode & 0o077
lines = p.read_text().splitlines()
assert 1 <= len(lines) <= 8
for line in lines:
    assert line.startswith('allow ') and line.endswith(';')
    value = line[6:-1]
    ipaddress.ip_address(value)
PY
"$nginx" -t
mkdir -m 700 -- "$backup"
[[ ! -f $target ]] || cp -p -- "$target" "$backup/target"
[[ ! -f $allowed ]] || cp -p -- "$allowed" "$backup/allowed"
cp -- "$bundle/REVISION" "$backup/REVISION"
touch "$backup/READY"
on_exit() {
    local rc=$?
    trap - EXIT
    if [[ $rc != 0 ]]; then restore_files; fi
    exit "$rc"
}
trap on_exit EXIT
install -d -m 700 /etc/aiops-dify-api
install -m 600 -- "$sources" "$allowed"
install -m 644 -- "$bundle/dify-service-api-TLS.conf" "$target"
"$nginx" -t
"$nginx" -s reload
echo 'installed; 公网及真实应用验收仍须单独执行'
