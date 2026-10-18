#!/usr/bin/env bash
# 起栈前准备模型出口的两个凭据文件（`up` 在 preflight 之后、compose 之前调用）。
#
# 两个凭据是**不同身份**，不能混用：
#   proxy_token       节点（经由平台下发的模型配置）出示给受控出口的凭据
#   upstream_api_key  受控出口出示给供应商的凭据；节点永远看不到它
#
# 密钥只写在 0600 文件里，不进命令行、不进日志。
set -euo pipefail

CONFIG=${M2LIVE_CONFIG:-/etc/aiops-m2-live}
GATEWAY="$CONFIG/gateway"
PROXY_TOKEN="$GATEWAY/proxy_token"
UPSTREAM_KEY="$GATEWAY/upstream_api_key"

mkdir -p "$GATEWAY"
umask 077

if [ ! -s "$PROXY_TOKEN" ]; then
  openssl rand -hex 32 > "$PROXY_TOKEN"
  echo "secrets: 生成节点→出口凭据"
fi
chmod 600 "$PROXY_TOKEN"

if [ ! -s "$UPSTREAM_KEY" ]; then
  if [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    printf 'synthetic-upstream-key\n' > "$UPSTREAM_KEY"
    echo "secrets: 合成模式，出口→上游用占位凭据"
  elif [ -n "${M2LIVE_MODEL_API_KEY_FILE:-}" ] && [ -r "${M2LIVE_MODEL_API_KEY_FILE}" ]; then
    cp "${M2LIVE_MODEL_API_KEY_FILE}" "$UPSTREAM_KEY"
    echo "secrets: 从 M2LIVE_MODEL_API_KEY_FILE 复制出口→上游凭据"
  else
    echo "secrets: 缺少供应商凭据（设置 M2LIVE_MODEL_API_KEY_FILE，或在合成模式运行）" >&2
    exit 2
  fi
fi
chmod 600 "$UPSTREAM_KEY"
