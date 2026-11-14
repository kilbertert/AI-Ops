#!/usr/bin/env bash
# 新版验收完成后的独立退休步骤；只移动已核定旧AI-Ops路径，不删除恢复副本。
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] && [ "$#" = 1 ] || exit 2
revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
backup=$(cat /var/lib/aiops-95/installed-backup)
[[ "$backup" == /var/backups/aiops-95/"$revision"/* ]] || exit 2
exec 9>/run/lock/aiops-95-install.lock
flock -n 9 || exit 2
[ "$(cat /var/lib/aiops-95/installed-revision)" = "$revision" ] || exit 2
systemctl is-active --quiet aiops-gateway-95
(cd "$backup" && sha256sum --check legacy.tar.sha256 >/dev/null)
python3 - "$backup" <<'PYPATH'
import pathlib,sys
for value in [sys.argv[1],'/var/lib/aiops-95','/home/aiops/AI-Ops','/home/aiops/gateway.env','/home/aiops/.config/aiops-diagnostics','/home/aiops/.local/share/aiops-diagnostics']:
    p=pathlib.Path(value)
    assert not any(x.is_symlink() for x in [p,*p.parents]), 'retirement path symlink refused'
PYPATH
[ ! -e "$backup/retired.complete" ] || { echo '旧实例已退休'; exit 0; }
install -d -m 700 "$backup/retired"
if systemctl cat aiops-gateway.service >/dev/null 2>&1; then systemctl disable --now aiops-gateway.service; fi
for path in /home/aiops/AI-Ops /home/aiops/gateway.env /home/aiops/.config/aiops-diagnostics /home/aiops/.local/share/aiops-diagnostics /etc/systemd/system/aiops-gateway.service; do
  parent="$backup/retired$(dirname "$path")"
  install -d -m 700 "$parent"
  destination="$parent/$(basename "$path")"
  if [ -e "$destination" ]; then
    [ ! -e "$path" ] || { echo '退休目标冲突，拒绝覆盖' >&2; exit 1; }
  else
    mv -- "$path" "$parent/"
  fi
done
systemctl daemon-reload
systemctl reset-failed aiops-gateway.service 2>/dev/null || true
: > "$backup/retired.complete"
printf '%s\n' '旧运行路径和服务已退休，恢复副本只保留在私有备份目录。'
