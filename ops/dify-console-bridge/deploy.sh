#!/usr/bin/env bash
# 在服务宿主安装已校验、已解压的合并制品；BACKUP 必须是新目录。
# 不改 env、systemd unit、登录代理、TLS 入口或 Dify 本身。
set -euo pipefail
umask 077
[ "$#" -eq 2 ] || { echo 'usage: deploy.sh BUNDLE BACKUP' >&2; exit 2; }
bundle=$(realpath -- "$1")
backup=$(realpath -m -- "$2")
code=/opt/aiops-console-bridge/aiops_diagnostics
js=/srv/aiops-console-assets/dify-publish-button.js
snippet=/www/server/panel/vhost/nginx/snippets/dify-console-publish.conf
service=aiops-dify-console-bridge.service
nginx=/www/server/nginx/sbin/nginx
test ! -e "$backup"
(cd "$bundle" && sha256sum --strict -c SHA256SUMS)
# 只带标准库依赖；隔离宿主 site-packages，证明不会偶然借用旧模块。
(cd "$bundle" && PYTHONDONTWRITEBYTECODE=1 python3 -S -c 'import aiops_diagnostics.dify_console_publish')
"$nginx" -t
systemctl is-active --quiet "$service"
mkdir -m 700 "$backup"
cp -a "$code" "$backup/aiops_diagnostics"
cp -a "$js" "$backup/dify-publish-button.js"
cp -a "$snippet" "$backup/dify-console-publish.conf"
cp "$bundle/REVISION" "$backup/NEW_REVISION"
restore() {
  systemctl stop "$service"
  rm -rf -- "$code"
  cp -a "$backup/aiops_diagnostics" "$code"
  cp -a "$backup/dify-publish-button.js" "$js"
  cp -a "$backup/dify-console-publish.conf" "$snippet"
  "$nginx" -t && "$nginx" -s reload
  systemctl start "$service"
}
on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then restore; fi
  exit "$rc"
}
trap on_exit EXIT
systemctl stop "$service"
rm -rf -- "$code"
cp -a "$bundle/aiops_diagnostics" "$code"
# Python 忽略旧字节码的时间戳碰撞；仅清理本服务的缓存。
find "$code" -type d -name __pycache__ -exec rm -rf -- {} +
find "$code" -type d -exec chmod 755 {} +
find "$code" -type f -exec chmod 644 {} +
install -m 644 "$bundle/dify-publish-button.js" "$js"
install -m 644 "$bundle/dify-console-publish.conf" "$snippet"
"$nginx" -t
systemctl start "$service"
"$nginx" -s reload
systemctl is-active --quiet "$service"
# 没有会话的请求必须由桥拒绝；若仍落进 Dify 的 HTML/404，部署不算成功。
for route in retract restore versions; do
  ready=false
  # reload 只发送信号：旧 worker 退出前仍可能返回旧路由的 HTML/404。
  for _attempt in {1..15}; do
    status=$(curl --max-time 2 -sS -o "$backup/probe.json" -w '%{http_code}' \
      -X POST "http://127.0.0.1:10008/__aiops/$route" \
      -H 'Content-Type: application/json' --data '{}') || status=000
    if [ "$status" = 403 ] && python3 -c \
      'import json,sys; b=json.load(open(sys.argv[1])); assert b["published"] is False and b["action"] == "" and b["reason"]' \
      "$backup/probe.json" 2>/dev/null; then
      ready=true
      break
    fi
    sleep 1
  done
  "$ready" || { echo "$route: 未就绪，最后 HTTP $status" >&2; exit 1; }
done
trap - EXIT
printf 'installed revision=%s backup=%s\n' "$(cat "$bundle/REVISION")" "$backup"
