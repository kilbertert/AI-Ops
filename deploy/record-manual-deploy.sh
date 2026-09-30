#!/usr/bin/env bash
# 手工部署留痕（#407）。
#
# 用法： deploy/record-manual-deploy.sh <commit> "<为什么走手工>"
#
# 为什么需要它：一次 CD 故障被一起隐藏了很久，因为同期有人在手工 rsync ——
# 生产确实更新了，于是没人去看 CD。**不可见的人工动作会连带隐藏本应被发现的异常。**
# 本脚本不阻拦手工部署（那是 CD 不可用时的应急路径），只要求它**留下可查的记录**。
#
# 记录落在两处，二者都不依赖当事人的记忆或聊天记录：
#   1. `deploy/manual-deploys.md` —— 随仓库走，评审时可见；
#   2. 目标主机上的运维记录（若可达）—— 让「这台机器上的这个版本怎么来的」就地可查。
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG="$REPO_ROOT/deploy/manual-deploys.md"
# 与 deploy-41.sh 用同一个默认身份别名；应急时可用 CD_SSH_ALIAS 覆盖。
CD_ALIAS=${CD_SSH_ALIAS:-aiops-41}
REMOTE_LOG=/var/lib/aiops-41/manual-deploys.log

die() { echo "错误：$*" >&2; exit 1; }

[ $# -ge 2 ] || die "用法：$0 <commit> \"<为什么走手工>\""
SHA=$1
REASON=$2

[ -f "$LOG" ] || die "找不到留痕文件：$LOG"

# 必须是一个真实存在于本仓的 commit —— 留痕的意义就是事后能查到"当时准备部署的是哪个修订"。
FULL=$(git -C "$REPO_ROOT" rev-parse --verify "${SHA}^{commit}" 2>/dev/null) \
  || die "commit 不存在或不在本仓历史里：$SHA"
SHORT=$(git -C "$REPO_ROOT" rev-parse --short=12 "$FULL")
WHEN=$(date -u +%Y-%m-%dT%H:%M:%SZ)
WHO=${USER:-unknown}

# 原因必须写清楚："应急"、"CD 挂了" 这类不构成事后可查的记录。
[ ${#REASON} -ge 10 ] || die "原因太短，写清楚为什么绕过 CD（至少 10 个字符）"

# 表格行：转义竖线，避免把表格结构撑坏。
SAFE_REASON=${REASON//|/\\|}
# shellcheck disable=SC2016  # 反引号是 Markdown 的字面量（包住 commit sha），**有意**不展开；
# 真正的替换走 %s 与双引号参数。
printf '| %s | `%s` | %s | %s | 待核对（部署后回填） |\n' "$WHEN" "$SHORT" "$WHO" "$SAFE_REASON" >> "$LOG"

echo "已留痕：$LOG"
echo "  时间 $WHEN / commit $SHORT / 操作者 $WHO"
echo "  原因 $REASON"
echo
echo "⚠️ 剩下两件事必须做完，否则留痕只有一半："
echo "  1. 部署完成后，回到 $LOG 把最后一行的事后核对填上；"
echo "  2. 确认这次部署没有抢在一个**正在等批准**的 CD run 前面 ——"
echo "     若是抢跑，先关掉那个 run 的审批请求，否则它被批时会按过期门禁被拒（或 worse）。"
echo "     查：gh run list --workflow=cd.yml --limit 5"

# 主机侧留痕：让「这台机器上的这个版本怎么来的」就地可查。
# **失败只警告**：主机的可达性不该决定仓库侧留痕是否成功。
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$CD_ALIAS" \
  "printf '%s %s %s %s\n' '$WHEN' '$SHORT' '$WHO' '$SAFE_REASON' >> '$REMOTE_LOG'" 2>/dev/null; then
  echo "已同步主机侧：$CD_ALIAS:$REMOTE_LOG"
else
  echo "⚠️ 主机侧留痕失败（$CD_ALIAS 不可达或不可写）—— 仓库侧已记，主机侧请手工补。" >&2
fi
