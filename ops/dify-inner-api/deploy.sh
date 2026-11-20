#!/usr/bin/env bash
# 升级已存在的自有转发脚本；原单位/端口/隧道/容器保持。
set -euo pipefail
umask 077
[[ $# == 3 ]] || { echo 'usage: deploy.sh install|restore BUNDLE BACKUP'; exit 2; }
action=$1
bundle=$(realpath -- "$2")
backup=$(realpath -m -- "$3")
target=/usr/local/bin/aiops-inner-api-forward
unit=aiops-inner-api-forward.service
exec 9>/run/lock/aiops-inner-api-forward.lock
flock -n 9 || { echo '转发器正在变更'; exit 1; }
[[ ! -L $target && -f $target ]] || { echo '只升级已存在的转发器'; exit 1; }
(cd "$bundle" && sha256sum --strict -c SHA256SUMS)
[[ $(cat "$bundle/REVISION") =~ ^[0-9a-f]{40}$ ]] || exit 1
# 接受已有端口的使用事实；有在飞连接时拒绝重启，不自动断开。
if ss -Hnt state established '( sport = :15001 )' | grep -q .; then
    echo '转发器仍有在飞连接，先等待结束'; exit 1
fi
health() {
    for attempt in {1..5}; do
        if systemctl is-active --quiet "$unit" &&
            [[ $(curl --max-time 10 -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:15001/console/api/all-workspaces) == 401 ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}
restore() {
    [[ -f $backup/READY && ! -L $backup/forwarder ]] || exit 1
    cp -p -- "$backup/forwarder" "$target"
    systemctl restart "$unit"
    health
}
if [[ $action == restore ]]; then restore; echo restored; exit; fi
[[ $action == install && ! -e $backup ]] || exit 2
health
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$bundle/aiops-inner-api-forward.py"
mkdir -m 700 -- "$backup"
cp -p -- "$target" "$backup/forwarder"
systemctl cat "$unit" > "$backup/unit.txt"
cp -- "$bundle/REVISION" "$backup/REVISION"
touch "$backup/READY"
on_exit() {
    rc=$?
    trap - EXIT
    if [[ $rc != 0 ]]; then restore; fi
    exit "$rc"
}
trap on_exit EXIT
install -m 0755 -- "$bundle/aiops-inner-api-forward.py" "$target"
systemctl restart "$unit"
health
echo 'installed; 长连接并发与业务链路仍须独立验收'
