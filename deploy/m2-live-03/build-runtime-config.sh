#!/usr/bin/env bash
# 从生产配置文件里**只取**隔离 Gateway 真正需要的只读业务依赖，写成一份独立文件。
#
# 为什么不再直接挂整份 production.env：Gateway 的设置读取顺序是「环境变量 or 配置文件」，
# 整份文件意味着试运行实例会继承生产里所有未覆盖的开关与凭据（路由、发布、媒体签名等），
# 隔离入口于是开放了超出试运行范围的接口。白名单比黑名单小得多，也更容易审查。
#
# 取哪些键：会话库、业务库（授权判定）、UPMS（C→B 映射与店铺归属）、TDengine（运行时
# 会构造，不配就默认值）、以及 thirdSession 服务令牌/前缀（身份解析）。
# **不取**：任何发布/媒体/路由分类器/模型供应商/SSH 的键。
#
# 用法（宿主上，读权限足够时）：
#   deploy/m2-live-03/build-runtime-config.sh <production.env> <输出路径>
set -euo pipefail

SOURCE=${1:?需要生产配置文件路径}
TARGET=${2:?需要输出路径}

[ -r "$SOURCE" ] || { echo "读不到 $SOURCE（需要该文件的读权限）" >&2; exit 2; }

ALLOW=(
  AIOPS_REDIS_HOST AIOPS_REDIS_PORT AIOPS_REDIS_DATABASE AIOPS_REDIS_USER AIOPS_REDIS_PASSWORD
  AIOPS_REDIS_MAX_MESSAGES
  AIOPS_MYSQL_HOST AIOPS_MYSQL_PORT AIOPS_MYSQL_USER AIOPS_MYSQL_PASSWORD AIOPS_MYSQL_DATABASE
  AIOPS_MYSQL_MAX_EXECUTION_MS
  AIOPS_UPMS_BASE_URL AIOPS_UPMS_INSIDE_TOKEN AIOPS_UPMS_TIMEOUT_SECONDS
  AIOPS_TDENGINE_URL AIOPS_TDENGINE_USER AIOPS_TDENGINE_PASSWORD AIOPS_TDENGINE_DATABASE
  AIOPS_TDENGINE_MAX_ROWS
  AIOPS_QUERY_TIMEOUT_SECONDS AIOPS_MAX_ORDER_WINDOW_HOURS
  AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX
)
# 明确**不取** SSH：试运行边界的语义是「只读直连依赖」，把 `AIOPS_SSH_ENABLED`
# 带进来会让诊断走跳板隧道，边界变成可配置的。

mkdir -p "$(dirname -- "$TARGET")"
umask 077
tmp=$(mktemp "${TARGET}.XXXXXX")

{
  echo "# 由 build-runtime-config.sh 从生产配置的**只读子集**生成；不要手工增加键。"
  echo "# 生成时间：$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for key in "${ALLOW[@]}"; do
    line=$(grep -m1 -E "^${key}=" "$SOURCE" || true)
    [ -n "$line" ] && printf '%s\n' "$line"
  done
} > "$tmp"

# 必须完整出现的键：缺一个就说明白名单与生产配置已经漂移，宁可拒绝也不留半个配置。
REQUIRED=(AIOPS_REDIS_HOST AIOPS_REDIS_PORT
          AIOPS_MYSQL_HOST AIOPS_MYSQL_PORT AIOPS_MYSQL_USER AIOPS_MYSQL_PASSWORD
          AIOPS_MYSQL_DATABASE AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN)
missing=()
for key in "${REQUIRED[@]}"; do
  grep -qE "^${key}=.+" "$tmp" || missing+=("$key")
done
if [ ${#missing[@]} -gt 0 ]; then
  rm -f "$tmp"
  echo "缺少必需的只读依赖: ${missing[*]}" >&2
  exit 1
fi

chmod 600 "$tmp"
mv "$tmp" "$TARGET"
echo "wrote $TARGET ($(grep -cE '^[A-Z]' "$TARGET") keys)"
