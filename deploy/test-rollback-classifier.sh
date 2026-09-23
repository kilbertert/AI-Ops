#!/usr/bin/env bash
# 回滚分类器的分支测试。
#
# 为什么单独测这一层：分类器的三个 bug（restart 非零时 set -e 直接终止、
# 标记写 stderr 而本机只捕获 stdout、只看 active 不看 /health）**都不是**
# 部署逻辑的问题，而是「主机输出的标记 → 本机的判断」这一层的问题。
# 它们在真机钻演里只暴露了一部分（第一次钻演恰好没触发 restart 非零那条）。
# 这里用构造的主机输出把每个分支都走一遍。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CLASSIFY="$SCRIPT_DIR/classify-remote-result.sh"

pass=0 fail=0
# check <name> <remote-rc> <want-code> <want-text> <remote-output>
check() {
  local name=$1 remote_rc=$2 want_code=$3 want_text=$4 out=$5
  local got code
  got=$(printf '%s' "$out" | REMOTE_RC="$remote_rc" "$CLASSIFY" 41 aiops-gateway-41.service 2>&1) && code=0 || code=$?
  if [ "$code" = "$want_code" ] && printf '%s' "$got" | grep -q "$want_text"; then
    printf '  PASS  %s\n' "$name"; pass=$((pass+1))
  else
    printf '  FAIL  %s\n    期望 code=%s 含 "%s"\n    实际 code=%s\n%s\n' \
      "$name" "$want_code" "$want_text" "$code" "$got"
    fail=$((fail+1))
  fi
}

# 1. 正常成功：无标记、退出码 0
check "成功（无标记）" 0 0 "部署成功" "selfcheck=passed
backup=/var/backups/aiops-41/backup-20260101-000000"

# 2. 自检失败但回滚成功，且 /health 版本与部署前一致
check "已自动回滚" 1 1 "已自动回滚" "selfcheck=failed
rollback=restored version=0.1.0+aaaa
backup=/b"

# 3. 回滚也失败 → 必须是 2（严重），并带上健康详情
check "回滚也失败" 1 2 "严重" "selfcheck=failed
rollback=also-failed
rollback-health=(no response)
rollback-active=inactive"

# 4. 关键回归：restart 非零时 printf 只输出这一行 ——
#    旧版靠 set -e 直接终止，本机什么标记都拿不到，会误报「状态未知」而不是「回滚失败」
check "restart 非零（旧版会漏报）" 1 2 "严重" "selfcheck=failed
rollback-restart-rc=nonzero
rollback=also-failed
rollback-active=inactive"

# 5. 超时 → 124，且提示回滚块没跑
check "超时" 124 124 "超时" ""

# 6. 什么都没输出（连接中断）→ 状态未知
check "无输出" 1 1 "状态未知" ""

# 7. 关键回归：只看 active 不算回滚成功。主机报 restored 但 health 版本≠部署前
#    ——分类器信任主机的判定，但主机那边现在要求 health 相等；这条验证版本行被转发
check "回滚成功带版本" 1 1 "0.1.0+bbbb" "selfcheck=failed
rollback=restored version=0.1.0+bbbb"

printf '\n回滚分类器：%s 通过，%s 失败\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
