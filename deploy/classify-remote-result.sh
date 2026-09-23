#!/usr/bin/env bash
# 把主机的部署输出分类成「成功 / 已回滚 / 回滚也失败 / 超时 / 未知」，并给出对应的
# 人工动作。抽成独立脚本是为了能对它做分支测试 —— 它的三个 bug（远端 set -e、
# 标记写 stderr、只看 active）都在这一层，而真机钻演只暴露了其中一部分。
#
# USAGE: classify-remote-result.sh <host> <service> < <remote-output>
# 输入从 stdin：远端 stdout + 本地判定的退出码由调用方通过 $REMOTE_RC 传入。
# 退出码：0 成功 / 1 已回滚或未知 / 2 回滚也失败 / 124 超时
set -euo pipefail

HOST_TARGET=${1:?host}
SERVICE=${2:?service}
REMOTE_RC=${REMOTE_RC:-0}
OUT=$(cat)

if [ "$REMOTE_RC" -eq 0 ]; then
  printf '部署成功\n'
  exit 0
fi

if [ "$REMOTE_RC" -eq 124 ]; then
  cat >&2 <<EOF
超时：远端命令被中途 kill，主机侧的自检与回滚块**没有机会执行**。
生产可能停在半途状态，须人工上机确认：
  ssh $HOST_TARGET 'systemctl is-active $SERVICE; curl -s http://127.0.0.1:8788/health'
EOF
  exit 124
fi

if printf '%s' "$OUT" | grep -q 'rollback=also-failed'; then
  RH=$(printf '%s\n' "$OUT" | sed -n 's/^rollback-health=//p' | head -1)
  RA=$(printf '%s\n' "$OUT" | sed -n 's/^rollback-active=//p' | head -1)
  cat >&2 <<EOF
严重：部署失败，且自动回滚后服务**未恢复到部署前状态**。
  service=${RA:-未知}
  /health=${RH:-无响应}
需立即人工介入：
  ssh $HOST_TARGET 'journalctl -u $SERVICE -n 80 --no-pager'
EOF
  exit 2
fi

if printf '%s' "$OUT" | grep -q 'rollback=restored'; then
  RV=$(printf '%s\n' "$OUT" | sed -n 's/^rollback=restored version=//p' | head -1)
  cat >&2 <<EOF
部署失败，但**主机已自动回滚**：源码与参考资料都恢复到部署前版本，服务 active 且
/health 报 ${RV:-部署前版本}（与部署前一致）。可用 --rollback-to <sha> 重试。
EOF
  exit 1
fi

printf '部署失败（退出码 %s），且未产生 selfcheck/rollback 标记 —— 状态未知，须人工确认 %s\n' \
  "$REMOTE_RC" "$HOST_TARGET" >&2
exit 1
