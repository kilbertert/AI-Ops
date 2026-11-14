#!/usr/bin/env bash
# 新版验收完成后的独立退休步骤；只移动已核定旧AI-Ops路径，不删除恢复副本。
set -euo pipefail
umask 077
[ "$(id -u)" = 0 ] && [ "$#" = 1 ] || exit 2
revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
backup=/var/backups/aiops-95/$revision
[ "$(cat /var/lib/aiops-95/installed-revision)" = "$revision" ] || exit 2
systemctl is-active --quiet aiops-gateway-95
(cd "$backup" && sha256sum --check legacy.tar.sha256 >/dev/null)
[ ! -e "$backup/retired" ] || { echo '旧实例已退休' >&2; exit 2; }
install -d -m 700 "$backup/retired"
systemctl disable --now aiops-gateway.service
for path in /home/aiops/AI-Ops /home/aiops/gateway.env /home/aiops/.config/aiops-diagnostics /home/aiops/.local/share/aiops-diagnostics /etc/systemd/system/aiops-gateway.service; do
  parent="$backup/retired$(dirname "$path")"
  install -d -m 700 "$parent"
  mv -- "$path" "$parent/"
done
systemctl daemon-reload
systemctl reset-failed aiops-gateway.service 2>/dev/null || true
printf '%s\n' '旧运行路径和服务已退休，恢复副本只保留在私有备份目录。'
