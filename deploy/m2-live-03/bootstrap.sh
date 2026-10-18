#!/usr/bin/env bash
# 平台初始化：把默认管理员口令换掉、禁用 schema 里的 demo 应用，然后用平台自己的
# 管理接口建本栈的应用/模型/智能体/OpenAPI 用户，并把 appId/agentId/token 回填进
# 绑定表。**可重复执行**：每一步先查现状，已经是目标状态就跳过。
#
# 为什么要这一步：schema 自带 admin/admin123 与 `snail-ai-agent-demo` 的固定 token。
# 在导入任何真实模型凭据或业务结果之前，这两个默认入口必须不可用。
#
# 用法（栈已 ready）：
#   deploy/m2-live-03/bootstrap.sh            # 幂等执行
#   deploy/m2-live-03/bootstrap.sh --check    # 只验证默认凭据已不可用
#
# 凭据去向：新管理员口令写入 $CONFIG/secrets/platform_admin（0600），不打印；
# 平台应用 token 只写进绑定表（0600），不打印。
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG=${M2LIVE_CONFIG:-/etc/aiops-m2-live}
PROJECT=${M2LIVE_PROJECT:-m2-live-03}
COMPOSE=(docker compose -f "$HERE/compose.yaml")
[ -n "${M2LIVE_COMPOSE_EXTRA:-}" ] && COMPOSE+=(-f "$M2LIVE_COMPOSE_EXTRA")
COMPOSE+=(--project-name "$PROJECT")
SERVICE=aiops-m2live03
MODE=apply
[ "${1:-}" = "--check" ] && MODE=check

work=$(mktemp -d "${TMPDIR:-/tmp}/m2live-bootstrap-XXXXXX")
trap 'rm -rf "$work"' EXIT

# 在 Gateway 容器里跑（它已经在 trial 网络上，并且有可达平台回环中继）。
# `docker cp` 保留源文件的属主。脚本以 root 运行时，复制进容器的文件是 root 的，
# 以服务 UID 运行的 Python 读不到（表现为「文件不存在」）。所以先复制进**临时目录**、
# 统一改属主，再放到位 —— 每一步都验证容器内可读。
copy_in() {  # copy_in <host-path> <container-path>
  local staged="$work/stage-$(basename "$2")"
  cp "$1" "$staged" && chmod 600 "$staged"
  chown "${M2LIVE_UID:-999}:${M2LIVE_GID:-987}" "$staged" 2>/dev/null || true
  docker cp "$staged" "m2live03-gateway:$2" >/dev/null
  docker exec m2live03-gateway sh -c "test -r '$2'" \
    || { echo "bootstrap: 容器内不可读: $2（属主不是服务 UID）" >&2; return 1; }
}

copy_in "$HERE/bootstrap_platform.py" /tmp/bootstrap_platform.py
copy_in "$CONFIG/gateway/agent-runs.json" /tmp/agent-runs.in.json

TOKEN_FILE="$CONFIG/secrets/platform_admin"
if [ -f "$TOKEN_FILE" ]; then
  copy_in "$TOKEN_FILE" /tmp/platform_admin
fi
# 节点凭据已存在就沿用：它同时以 SHA-256 出现在绑定表里，换了就得两边一起换。
NODE_TOKEN=$(sed -n 's/^AIOPS_NODE_TOKEN=//p' "$CONFIG/node/node.env" 2>/dev/null || true)
if [ -z "$NODE_TOKEN" ]; then
  NODE_TOKEN=$(openssl rand -hex 32)
fi
# 令牌不进 `docker exec -e`（那会出现在进程参数里，宿主上任何能读 /proc 的人都能看到）。
# 写进容器内的 0600 临时文件，由脚本自己读走。
printf '%s\n' "$NODE_TOKEN" > "$work/node_token"
copy_in "$work/node_token" /tmp/node_token
# 模型端点与凭据：端点走环境变量（不是秘密），凭据走 0600 文件。两者都必须显式给出
# ——正式模式不接受合成桩的默认端点。
# `--check` 只验证默认凭据已不可用，不建模型，因此不需要端点与密钥。
MODEL_ENDPOINT=""
MODEL_ID=""
if [ "$MODE" = "apply" ]; then
  # 正式模式的端点**必须**是受控出口（model-proxy），不是供应商本体：
  # 调用发生在节点进程里，而节点没有任意外联。
  MODEL_ENDPOINT=${M2LIVE_MODEL_ENDPOINT:-}
  MODEL_ID=${M2LIVE_MODEL_ID:-}
  if [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    # 平台下发给节点的端点是**受控出口的源**；OpenAI 兼容客户端自己追加
    # /v1/chat/completions，出口按白名单转发同一路径。
    MODEL_ENDPOINT=${MODEL_ENDPOINT:-https://model-proxy:9101}
    MODEL_ID=${MODEL_ID:-m2live03-synthetic}
  fi
  if [ -z "$MODEL_ENDPOINT" ] || [ -z "$MODEL_ID" ]; then
    echo "bootstrap: 正式模式需要 M2LIVE_MODEL_ENDPOINT（受控出口）与 M2LIVE_MODEL_ID（供应商模型标识）" >&2
    exit 2
  fi
  # 平台下发给节点的凭据是**出口专用令牌**，不是供应商 key：节点只应能调用受控出口，
  # 供应商凭据只存在于出口容器里。两者由 prepare-secrets.sh 分别生成。
  MODEL_KEY=${M2LIVE_MODEL_API_KEY_FILE:-$CONFIG/gateway/proxy_token}
  if [ ! -r "$MODEL_KEY" ]; then
    echo "bootstrap: 读不到模型出口令牌 $MODEL_KEY（先跑 checks.sh up）" >&2
    exit 2
  fi
  cp "$MODEL_KEY" "$work/model_api_key"
  chmod 600 "$work/model_api_key"
  copy_in "$work/model_api_key" /tmp/model_api_key
fi

set +e
docker exec \
  -e "M2LIVE_SERVICE=$SERVICE" \
  -e "M2LIVE_BOOTSTRAP_MODE=$MODE" \
  -e "M2LIVE_SYNTH_MODE=${M2LIVE_SYNTH_MODE:-0}" \
  -e "M2LIVE_MODEL_ENDPOINT=$MODEL_ENDPOINT" \
  -e "M2LIVE_MODEL_ID=$MODEL_ID" \
  m2live03-gateway python /tmp/bootstrap_platform.py
rc=$?
set -e

if [ $rc -eq 0 ] && [ "$MODE" = "apply" ]; then
  # 新口令与新绑定表都带出来：容器是一次性的，文件才是状态。
  docker cp m2live03-gateway:/tmp/platform_admin "$TOKEN_FILE.new" >/dev/null
  chmod 600 "$TOKEN_FILE.new" && mv "$TOKEN_FILE.new" "$TOKEN_FILE"
  docker cp m2live03-gateway:/tmp/agent-runs.out.json "$work/agent-runs.json" >/dev/null
  chmod 600 "$work/agent-runs.json"
  mv "$work/agent-runs.json" "$CONFIG/gateway/agent-runs.json"
  docker cp m2live03-gateway:/tmp/node.env.out "$work/node.env" >/dev/null
  chmod 600 "$work/node.env"
  if cmp -s "$work/node.env" "$CONFIG/node/node.env" 2>/dev/null; then
    echo "bootstrap: 节点凭据未变化，不重启节点"
  else
    mv "$work/node.env" "$CONFIG/node/node.env"
    # 节点只在**容器创建时**读取自己的 env_file：`docker restart` 沿用旧环境，
    # 心跳会一直失败。必须重建容器。冷启动顺序因此是
    # `up`（节点 pending）→ `bootstrap`（重建节点）→ `ready`。
    "${COMPOSE[@]}" up -d --force-recreate node >/dev/null
    echo "bootstrap: 节点凭据已更新，节点容器已重建"
  fi
  echo "bootstrap: ok（绑定表与节点凭据已回填；管理员口令在 $TOKEN_FILE）"
elif [ $rc -eq 0 ]; then
  echo "bootstrap: 默认凭据已不可用"
fi
exit $rc
