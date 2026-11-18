#!/usr/bin/env bash
# 仅在规范开发checkout安装本项目用户timer；服务宿主不运行开发源或保存SSH私钥。
set -euo pipefail
umask 077
[[ $# == 1 && ( $1 == install || $1 == stop ) ]] || { echo 'usage: install-worker.sh install|stop'; exit 2; }
[[ $(id -un) == claude && $(git rev-parse --show-toplevel) == /home/claude/Projects/AI-Ops ]] || exit 2
if [[ $1 == stop ]]; then
  systemctl --user disable --now aiops-site-preflight.timer
  # 不终止在飞service；只停止下次轮询，现有租约/回执继续核对。
  exit
fi
[[ -z $(git status --porcelain) ]] || exit 2
git merge-base --is-ancestor HEAD origin/main
base=$(git rev-parse --show-toplevel)
unit_dir=$HOME/.config/systemd/user
backup=$HOME/.cache/aiops-site-preflight-units/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$unit_dir"
[[ ! -e $backup ]] || exit 2
mkdir -p "$backup"
names=(aiops-site-preflight.service aiops-site-preflight.timer)
for name in "${names[@]}"; do
  [[ ! -L $unit_dir/$name ]] || exit 2
  if [[ -e $unit_dir/$name ]]; then cp -p "$unit_dir/$name" "$backup/$name"; fi
done
was_enabled=false
was_active=false
if systemctl --user is-enabled --quiet aiops-site-preflight.timer; then was_enabled=true; fi
if systemctl --user is-active --quiet aiops-site-preflight.timer; then was_active=true; fi
restore() {
  systemctl --user disable --now aiops-site-preflight.timer || true
  for name in "${names[@]}"; do
    if [[ -e $backup/$name ]]; then cp -p "$backup/$name" "$unit_dir/$name"; else rm -f "$unit_dir/$name"; fi
  done
  systemctl --user daemon-reload
  if "$was_enabled"; then systemctl --user enable aiops-site-preflight.timer; fi
  if "$was_active"; then systemctl --user start aiops-site-preflight.timer; fi
}
rollback() {
  local result=$?
  trap - EXIT
  if [[ $result != 0 ]]; then restore; fi
  exit "$result"
}
trap rollback EXIT
for name in "${names[@]}"; do
  install -m 600 "$base/ops/environments/gateway/$name" "$unit_dir/$name"
done
systemd-analyze --user verify "$unit_dir/aiops-site-preflight.service" "$unit_dir/aiops-site-preflight.timer"
systemctl --user daemon-reload
systemctl --user enable --now aiops-site-preflight.timer
trap - EXIT
printf 'installed source=%s backup=%s\n' "$(git rev-parse HEAD)" "$backup"
