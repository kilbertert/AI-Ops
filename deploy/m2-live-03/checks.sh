#!/usr/bin/env bash
# M2-LIVE-03 受控试运行栈的起停与检查。
#
# 子命令：
#   preflight   只读检查：宿主余量、端口、目录、密钥权限、镜像身份、制品摘要、隔离门禁
#   up          先 preflight，全部通过才起栈；起栈后再跑 ready
#   ready       就绪：store 健康、platform UP、本 app 节点已注册、Gateway /health
#   verify      结构验证：/health、无会话 401、委派无会话 401、运行身份
#   deps        依赖可达性（从隔离 Gateway 的网络空间；真实请求之前的门）
#   status      容器状态与资源占用
#   down        停栈（保留数据）；`down --purge` 才删栈数据
#
# 退出码：0 通过；1 有检查失败；2 前置不足；3 拒绝执行（所有权/隔离判定不成立）。
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DIST=${M2LIVE_DIST:-/opt/aiops-m2-live}
CONFIG=${M2LIVE_CONFIG:-/etc/aiops-m2-live}
STATE=${M2LIVE_STATE:-/var/lib/aiops-m2-live}
PROJECT=${M2LIVE_PROJECT:-m2-live-03}
COMPOSE=(docker compose -f "$HERE/compose.yaml")
[ -n "${M2LIVE_COMPOSE_EXTRA:-}" ] && COMPOSE+=(-f "$M2LIVE_COMPOSE_EXTRA")
COMPOSE+=(--project-name "$PROJECT")

# 栈的容器名带项目后缀之外的前缀，所有按名字的操作都必须先确认「这是本项目的」。
containers() { printf '%s\n' m2live03-store m2live03-platform m2live03-node m2live03-gateway m2live03-host-relay m2live03-model-proxy; }
project_of() {  # project_of <container> -> compose project label（不存在则空）
  docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$1" 2>/dev/null
}

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
say() { printf '%s\n' "$*"; }

fail=0
ok() { say "  ok   $*"; }
bad() { red "  FAIL $*"; fail=1; }

lock_value() {  # lock_value <section> <key>
  python3 - "$HERE/images.lock.json" "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d[sys.argv[2]][sys.argv[3]] or "")
PY
}

registry_value() {  # registry_value <section> <key>（无该段/键时为空）
  python3 - "$HERE/images.lock.json" "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print((d.get(sys.argv[2]) or {}).get(sys.argv[3], ""))
PY
}

# ---------------------------------------------------------------- 隔离门禁
# 与真实启动路径共用同一份判据：`isolation_guard` 就是 gateway_server serve 调用的那个
# 模块。这里在**同一个镜像、同一个 UID、同一组挂载**下跑它，因此「检查通过」与
# 「启动时通过」是同一件事，而不是两份相似的判断。
gateway_image() { "${COMPOSE[@]}" config --images 2>/dev/null | grep -m1 'aiops-m2-live-03-gateway' || true; }

guard() {
  local image uid gid
  image=$(gateway_image)
  [ -n "$image" ] || { red "找不到 Gateway 镜像名"; return 1; }
  uid=${M2LIVE_UID:-999}; gid=${M2LIVE_GID:-987}
  # 与真实容器同镜像、同 UID、同一组挂载、同一份 gateway.env：判据就是启动时那份。
  docker run --rm \
    --user "$uid:$gid" \
    --env-file "$CONFIG/gateway/gateway.env" \
    -v "$STATE/gateway:/data" \
    -v "$CONFIG/gateway:/etc/m2live:ro" \
    -v "$CONFIG/gateway/runtime.env:/etc/m2live/runtime.env:ro" \
    -v "$CONFIG/tls/public:/etc/m2live-tls:ro" \
    -e "M2LIVE_ISOLATION_ROOT=/data" \
    -e "M2LIVE_EXPECTED_UID=$uid" \
    -e "M2LIVE_READONLY_PATHS=/etc/m2live/runtime.env:/etc/m2live:/etc/m2live-tls" \
    "$image" python -m aiops_diagnostics.gateway.isolation_guard
}

preflight() {
  fail=0
  say "== 工具 =="
  docker version >/dev/null 2>&1 && ok "docker" || bad "docker 不可用"
  docker compose version >/dev/null 2>&1 && ok "docker compose v2" || bad "docker compose v2 不可用"

  say "== 宿主余量（只报告，不自动改限额） =="
  local avail_kb disk_kb
  avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  disk_kb=$(df -Pk "$STATE" 2>/dev/null | awk 'NR==2{print $4}')
  say "  内存可用 $((avail_kb / 1024)) MiB"
  say "  $STATE 所在盘余量 $(( ${disk_kb:-0} / 1024 )) MiB"
  if [ "$avail_kb" -lt $((3892 * 1024)) ]; then
    bad "预算不足：需要 ≥3892 MiB 可用（栈 2992 + 宿主余量 900），实际 $((avail_kb / 1024)) MiB"
  else
    ok "余量够整栈预算"
  fi

  say "== 宿主端口（只允许回环） =="
  if ss -lnt 2>/dev/null | grep -qE ':8794[[:space:]]'; then
    if [ "$(project_of m2live03-gateway)" = "$PROJECT" ]; then
      ok "8794 由本栈的隔离 Gateway 占用"
    else
      bad "8794 已被占用，且不是本栈 —— 无法确认是谁在监听"
    fi
  else
    ok "8794 空闲"
  fi

  say "== 目录 =="
  local d
  for d in "$DIST/platform" "$DIST/node" \
           "$CONFIG/gateway" "$CONFIG/node" "$CONFIG/platform" "$CONFIG/secrets" \
           "$CONFIG/model-proxy" \
           "$STATE/store" "$STATE/gateway" "$STATE/node"; do
    [ -d "$d" ] && ok "$d" || bad "缺目录 $d"
  done

  say "== 密钥与配置权限（要求 600 或 400） =="
  local f mode
  for f in "$CONFIG/secrets/mysql_root" "$CONFIG/gateway/gateway.env" \
           "$CONFIG/node/node.env" "$CONFIG/platform/application.properties" \
           "$CONFIG/gateway/agent-runs.json"; do
    if [ ! -f "$f" ]; then bad "缺文件 $f"; continue; fi
    mode=$(stat -c '%a' "$f")
    case "$mode" in
      600|400) ok "$f ($mode)" ;;
      *) bad "$f 权限 $mode（要求 600/400）" ;;
    esac
  done

  say "== 隔离门禁（与实际启动路径同一判据） =="
  # 状态根、可写路径、只读依赖、属主；任何一条不成立都拒绝起栈。
  local guard_out guard_rc
  guard_out=$(guard 2>&1) && guard_rc=0 || guard_rc=$?
  if [ $guard_rc -eq 0 ]; then
    ok "状态根与可写路径在 $STATE 之内"
  else
    bad "隔离门禁拒绝：$guard_out"
  fi

  say "== 全部可写 bind mount（按解析后的路径，不是字符串前缀） =="
  # 只验顶层字符串会漏掉「STATE 本身没问题、但某一跳挂到了别处」。这里读**渲染后**的
  # compose，逐条核对 source：可写必须在状态根之内，只读必须在白名单里。
  local mount_report mount_rc rendered_json
  if ! rendered_json=$("${COMPOSE[@]}" config --format json 2>/dev/null); then
    bad "compose 渲染失败（live 模式不接受：缺变量时起起来的不是这一份）"
    rendered_json='{}'
  fi
  mount_report=$(printf '%s' "$rendered_json" | python3 "$HERE/stack_checks.py" mounts \
    "$STATE" "$CONFIG" \
    "$DIST/platform" "$DIST/node" "$DIST/gateway-src" "$HERE/schema") && mount_rc=0 || mount_rc=$?
  if [ $mount_rc -eq 0 ]; then
    ok "所有可写 bind mount 都在 $STATE 之内；只读挂载都在白名单内"
  else
    bad "挂载边界不成立：$mount_report"
  fi

  say "== 运行身份（渲染后的 uid/gid，不是容器里的默认用户） =="
  local want_uid=${M2LIVE_UID:-$(id -u)} want_gid=${M2LIVE_GID:-$(id -g)}
  say "  期望 ${want_uid}:${want_gid}"
  local user_report user_rc
  user_report=$(printf '%s' "$rendered_json" | python3 "$HERE/stack_checks.py" users \
    "${want_uid}:${want_gid}") && user_rc=0 || user_rc=$?
  if [ $user_rc -eq 0 ]; then
    ok "platform/node/gateway/host-relay/model-proxy 都显式指定运行 UID"
  else
    bad "运行 UID 未显式指定或与期望不符: $user_report"
  fi

  say "== 状态目录属主（服务 UID 必须能写，且不是 root 的） =="
  # 平台库由官方 mysql 镜像的入口点管理：数据目录属主是镜像里的 mysql（999），
  # 这是该镜像的既定模型。其余目录必须属于本栈服务 UID。
  local want_owner=${M2LIVE_UID:-999} owner_problem=""
  for d in "$STATE/gateway" "$STATE/node" "$STATE/platform"; do
    [ -d "$d" ] || { owner_problem+=" 缺目录 $d"; continue; }
    [ "$(stat -c '%u' "$d")" = "$want_owner" ] || owner_problem+=" $d 属主 $(stat -c '%u' "$d")≠$want_owner"
  done
  if [ -d "$STATE/store" ]; then
    case "$(stat -c '%u' "$STATE/store")" in
      "$want_owner"|999) ;;
      *) owner_problem+=" store 属主 $(stat -c '%u' "$STATE/store") 既不是 $want_owner 也不是 mysql(999)" ;;
    esac
  else
    owner_problem+=" 缺目录 $STATE/store"
  fi
  [ -z "$owner_problem" ] && ok "状态目录属主符合（服务 UID $want_owner；store 允许 mysql 999）" \
    || bad "属主不符:$owner_problem"

  say "== 基础镜像身份（按 image ID，不按 tag；覆盖参数绕不开） =="
  local ref want_id got_id
  while read -r ref; do
    [ -n "$ref" ] || continue
    # Gateway 是**构建产物**，它有自己的段落与内容摘要标签，下面单独核对。
    case "$ref" in *aiops-m2-live-03-gateway*) continue ;; esac
    if ! got_id=$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null); then
      bad "缺镜像 $ref"; continue
    fi
    want_id=$(python3 - "$HERE/images.lock.json" "$ref" <<'PY'
import json, sys
refs = json.load(open(sys.argv[1])).get("base_images", {}).get("refs", {})
print((refs.get(sys.argv[2]) or {}).get("image_id", ""))
PY
)
    if [ -z "$want_id" ]; then
      bad "$ref 不在锁文件里（镜像覆盖参数不能绕开锁定）"
    elif [ "$got_id" != "$want_id" ]; then
      bad "$ref 身份不符（锁 ${want_id:7:12}…，实际 ${got_id:7:12}…）"
    else
      ok "$ref ${got_id:7:12}…"
    fi
  done < <("${COMPOSE[@]}" config --images 2>/dev/null | sort -u)

  say "== 制品摘要（对照 images.lock.json） =="
  local path want got name
  for name in platform node; do
    case $name in
      platform) path=$DIST/platform/snail-ai-server-exec.jar ;;
      node) path=$DIST/node/aiops-execution-node.jar ;;
    esac
    want=$(lock_value "$name" sha256)
    if [ ! -f "$path" ]; then bad "缺制品 $path"; continue; fi
    if [ -z "$want" ]; then bad "images.lock.json 里 $name 没有摘要"; continue; fi
    got=$(sha256sum "$path" | awk '{print $1}')
    [ "$got" = "$want" ] && ok "$name ${got:0:16}…" || bad "$name 摘要不符（期望 ${want:0:16}…，实际 ${got:0:16}…）"
  done

  say "== M3 执行配置清单（两侧同一份字节） =="
  if [ -f "$CONFIG/gateway/identity.env" ] || [ -L "$CONFIG/gateway/identity.env" ]; then
    mode=$(stat -c '%a' "$CONFIG/gateway/identity.env")
    if [ -L "$CONFIG/gateway/identity.env" ] || { [ "$mode" != 600 ] && [ "$mode" != 400 ]; }; then
      bad "构造身份文件必须是私有普通文件（0400/0600）"
    fi
  fi
  # 配了 `AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE` 却读不到 ⇒ Gateway **启动即拒**。
  # 判据不是「两个路径的 basename 相同」：那对「节点开关缺失」和「路径指向不存在的
  # 文件」都印 PASS（旧实现的实测反例）。
  #
  # 值取自**渲染后的 environment**，不是配置文件原文：覆盖层（`M2LIVE_COMPOSE_EXTRA`）
  # 可以在不改任何文件的情况下让实际那个容器单侧开启，而读原文会看到「两侧都开着」。
  # 解析与同 inode 判定在 stack_checks.py：容器内路径 → 渲染后的挂载 → 宿主路径。
  local manifest_report manifest_rc
  manifest_report=$(printf '%s' "$rendered_json" | python3 "$HERE/stack_checks.py" manifest) \
    && manifest_rc=0 || manifest_rc=$?
  if [ $manifest_rc -eq 0 ]; then
    ok "$manifest_report"
  else
    bad "$manifest_report"
  fi

  say "== Gateway 镜像身份（内容摘要为主，加载身份为辅） =="
  # 镜像 ID 含构建时间戳，每次重建都不同，所以它不在仓库里；**内容摘要**（与 uv.lock
  # 绑定）才是可复现的身份。宿主上若有 build-image.sh 写出的 identity.json，再核对
  # 实际加载的 ID —— 两者任一不符即拒绝。
  local gw_ref gw_id src want_src want_id
  gw_ref=$(gateway_image)
  gw_id=$(docker image inspect -f '{{.Id}}' "$gw_ref" 2>/dev/null || true)
  want_src=$(registry_value gateway_image source_sha256)
  if [ -z "${gw_id:-}" ]; then
    bad "Gateway 镜像不存在（先跑 build-image.sh 并在宿主 load）"
  else
    src=$(docker image inspect -f '{{ index .Config.Labels "m2live.source" }}' "$gw_ref" 2>/dev/null)
    if [ -z "$want_src" ]; then
      bad "images.lock.json 的 gateway_image.source_sha256 为空 —— 未记录内容身份"
    elif [ "$src" != "$want_src" ]; then
      bad "Gateway 内容摘要不符（锁 ${want_src:0:16}…，镜像标签 ${src:-（无）}）"
    else
      ok "Gateway 内容摘要 ${src:0:16}… 与 uv.lock 绑定"
    fi
    # 内容摘要标签**可以**被复制到替换镜像上（它是磁盘上的标签，不是镜像身份）。
    # 唯一不可伪造的是宿主上 build-image.sh 写出的 identity.json：里面是那次构建的
    # image ID。没有它就无法证明跑的是构建产物，所以 preflight 拒绝，而不是「只核对标签」。
    local identity=$DIST/gateway-image/identity.json
    if [ ! -f "$identity" ]; then
      bad "缺 $identity：无法证明 Gateway 镜像是构建产物（标签可被复制）"
    else
      want_id=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image_id"])' "$identity")
      if [ "$gw_id" != "$want_id" ]; then
        bad "Gateway 镜像 ID 与加载记录不符（记录 ${want_id#sha256:}，实际 ${gw_id#sha256:}）"
      else
        ok "Gateway 镜像 ID ${gw_id:7:12}… 与加载记录一致"
      fi
    fi
  fi

  say "== 模型出口（固定目标，必填） =="
  if [ -n "${M2LIVE_MODEL_UPSTREAM:-}" ]; then
    ok "上游目标已给出（不在此打印值）"
  else
    bad "M2LIVE_MODEL_UPSTREAM 未设置：模型调用没有受控出口"
  fi
  if [ -n "${M2LIVE_MODEL_ID:-}" ] || [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    ok "供应商模型标识已给出（合成模式有默认）"
  else
    bad "M2LIVE_MODEL_ID 未设置：正式模式必须有供应商模型标识"
  fi

  say "== 路由归属 =="
  local prod_env=/etc/aiops-41/gateway.env
  # 本栈的三个开关必须**显式非空**：Gateway 读取顺序是「环境变量 or 生产配置文件」，
  # 空值会静默回退到生产文件里的同名键 —— 那正是「试运行实例继承生产路由」的形状。
  local env_file=$CONFIG/gateway/gateway.env missing_switch=""
  for key in AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL AIOPS_GATEWAY_EXECUTION_ENVIRONMENT \
             AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE; do
    if ! grep -qE "^${key}=.+" "$env_file" 2>/dev/null; then
      missing_switch+=" $key"
    fi
  done
  if [ -n "$missing_switch" ]; then
    bad "本栈开关为空，会从生产配置继承:$missing_switch"
  else
    ok "本栈三个开关都显式非空（不会回退到生产配置）"
  fi
  if [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    # 合成模式明说：生产开关是否关闭不是本模式能证明的，单独列出、不判 PASS。
    say "  SYNTHETIC 生产开关未核对（合成模式不接触生产配置）"
  elif [ -r "$prod_env" ]; then
    if grep -qE '^AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=.+' "$prod_env"; then
      bad "生产 Gateway 的直读开关已启用 —— 本栈不再是隔离启用点"
    else
      ok "生产直读开关仍关闭"
    fi
  else
    bad "读不到 $prod_env：无法证明生产开关仍关闭（live 模式不接受跳过）"
  fi

  return $((fail == 0 ? 0 : 1))
}

up() {
  preflight || { red "preflight 未通过，未启动任何容器"; return 2; }
  # 所有权标记只有在预检通过之后才写：它是 `down --purge` 唯一的判据，写在一个
  # 随后被拒绝的状态根上，会把别人的目录变成「本栈的」。原子创建（临时文件 + mv），
  # 且绝不覆盖不属于本项目的标记。
  claim_state_root || return $?
  # 模型出口的两个凭据（节点→出口、出口→供应商）。缺供应商凭据时在这里失败，
  # 而不是等第一次模型调用。
  "$HERE/prepare-secrets.sh" || return 2
  say "== 启动 =="
  "${COMPOSE[@]}" up -d || return 2
  ready
}

ready() {
  say "== 就绪检查（最多 240s） =="
  local deadline=$((SECONDS + 240)) gateway=0 store=0 platform=0 node=0
  while [ $SECONDS -lt $deadline ]; do
    gateway=0; store=0; platform=0; node=0
    curl -fsS -m 3 http://127.0.0.1:8794/health >/dev/null 2>&1 && gateway=1
    [ "$(docker inspect -f '{{.State.Health.Status}}' m2live03-store 2>/dev/null)" = healthy ] && store=1
    docker exec -i -e "M2LIVE_PLATFORM_URL=${M2LIVE_PLATFORM_URL:-https://platform:8900}" \
      m2live03-gateway python - >/dev/null 2>&1 <<'PY' && platform=1 || platform=0
import json, os, urllib.request
url = os.environ["M2LIVE_PLATFORM_URL"].rstrip("/") + "/snail-ai/actuator/health"
body = json.loads(urllib.request.urlopen(url, timeout=5).read())
raise SystemExit(0 if body.get("status") == "UP" else 1)
PY
    # 节点注册是「平台和节点都真的在工作」的证据，不是 /health 能替代的。
    # 只读查询平台自己的元数据库；注册本身仍由节点的心跳完成。
    local registered
    registered=$(docker exec -e "M2LIVE_CHECK_APP=${M2LIVE_APP_ID:-aiops-m2live03}" m2live03-store sh -c \
      'mysql -uroot -p"$(cat /run/secrets/mysql_root)" --skip-column-names \
         -e "SELECT COUNT(*) FROM snail_ai.sai_client_node \
             WHERE expire_dt > NOW() AND app_id = \"${M2LIVE_CHECK_APP}\""' 2>/dev/null \
      | tr -d '[:space:]')
    if [ -n "${registered:-}" ] && [ "$registered" -gt 0 ] 2>/dev/null; then node=1; fi
    [ $gateway -eq 1 ] && [ $store -eq 1 ] && [ $platform -eq 1 ] && [ $node -eq 1 ] && break
    sleep 4
  done
  local missing=""
  # 冷启动顺序：首次 up 时节点还没有平台应用凭据（要等 bootstrap）。这**不是**就绪：
  # 报告 pending 并返回非零，`ready` 的成功只表示「四项在同一轮都成立」。
  # 把 pending 当成通过会让「节点已注册」这句话在什么都没注册时也成立。
  # 「模板只有键名」不等于「凭据已生成」：判据是 appId **有值**，不是文件非空。
  local node_env="${M2LIVE_CONFIG:-/etc/aiops-m2-live}/node/node.env"
  if [ $node -eq 0 ] && ! grep -qE '^SNAIL_AI_APP_ID=.+' "$node_env" 2>/dev/null; then
    red "  pending node：节点凭据尚未生成 —— 先跑 bootstrap.sh，再跑 checks.sh ready"
    return 1
  fi
  [ $store -eq 1 ] || missing+=" store"
  [ $platform -eq 1 ] || missing+=" platform"
  [ $node -eq 1 ] || missing+=" node"
  [ $gateway -eq 1 ] || missing+=" gateway"
  if [ -n "$missing" ]; then
    red "  未就绪:$missing"
    "${COMPOSE[@]}" ps
    local c
    for c in $(containers); do
      say "--- $c"
      docker logs --tail 20 "$c" 2>&1 | sed 's/^/    /'
    done
    return 1
  fi
  grn "  四项就绪：store / platform / 节点注册 / 隔离 Gateway"
  say "== 实际占用（对照预算 2992 MiB / 1.49 CPU） =="
  docker stats --no-stream --format '  {{.Name}}  mem={{.MemUsage}}  cpu={{.CPUPerc}}' \
    $(containers) 2>/dev/null
}

verify() {
  local base=http://127.0.0.1:8794 code
  say "== 结构验证（不需要真实身份） =="
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$base/health")
  [ "$code" = "200" ] && grn "  /health 200" || { red "  /health $code"; return 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$base/v1/orders/X/charging-status")
  [ "$code" = "401" ] && grn "  无会话 401" || { red "  无会话应为 401，实际 $code"; return 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST \
    -H 'X-Business-Entry: consumer' -H 'Content-Type: application/json' -d '{}' \
    "$base/v1/orders/X/charging-status/delegated")
  [ "$code" = "401" ] && grn "  委派无会话 401" || { red "  委派无会话应为 401，实际 $code"; return 1; }

  # 运行身份：不是 root，且各服务一致。store 单独判定 —— 官方 mysql 镜像的入口点
  # 以 root 启动、随后把 mysqld 降到 mysql(999)，那是该镜像的既定模型。
  local want_uid=${M2LIVE_UID:-$(id -u)} c uid
  for c in m2live03-platform m2live03-node m2live03-gateway m2live03-host-relay m2live03-model-proxy; do
    uid=$(docker exec "$c" id -u 2>/dev/null)
    if [ "$uid" != "$want_uid" ]; then
      red "  $c 以 uid ${uid:-?} 运行，期望 $want_uid"
      return 1
    fi
  done
  uid=$(docker exec m2live03-store sh -c 'id -u mysql' 2>/dev/null)
  if [ "$uid" != "999" ]; then
    red "  store 的 mysql 用户 uid=${uid:-?}，期望 999（官方镜像的固定值）"
    return 1
  fi
  grn "  五个服务以 uid $want_uid 运行；store 的 mysqld 以 mysql(999)"

  # 默认凭据：平台起来之后、任何人发真实请求之前，schema 自带的 admin/admin123 与
  # demo 应用必须已经被处置。这里直接复用 bootstrap 的 --check，不另写一份判断。
  if [ -x "$HERE/bootstrap.sh" ]; then
    if M2LIVE_CONFIG="$CONFIG" "$HERE/bootstrap.sh" --check >/dev/null 2>&1; then
      grn "  默认管理员口令与 demo 应用已不可用"
    else
      red "  默认凭据仍可用 —— 先跑 bootstrap.sh，再验证"
      return 1
    fi
  fi

  # 无出网：节点只接两张 internal 网络。判据必须有**正对照**，否则一个根本连不上的
  # 客户端会让「禁止目标不可达」变成空话：基础 JVM 镜像的 /bin/sh 是 dash，它没有
  # /dev/tcp —— 上一版正是因此把「从未发起连接」报成了「隔离成功」。
  # 这里用镜像里确实存在的 bash（/bin/bash）：
  #   正对照 model-proxy:9101 必须可达（节点到受控出口）
  #   禁止目标 1.1.1.1:443 必须不可达（节点没有任意外联）
  local probe=/bin/bash
  docker exec m2live03-node "$probe" -c 'true' 2>/dev/null || {
    red "  节点里没有可用的 bash，无法做正对照 —— 不接受「无出网」的结论"
    return 1
  }
  if ! docker exec m2live03-node "$probe" -c 'exec 3<>/dev/tcp/model-proxy/9101' 2>/dev/null; then
    red "  正对照失败：节点连不到受控模型出口 model-proxy:9101"
    return 1
  fi
  if docker exec m2live03-node "$probe" -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then
    red "  节点容器仍能建立外网 TCP 连接 —— internal 网络未生效"
    return 1
  fi
  grn "  节点可达受控模型出口、不可达外网（正对照成立）"
}

# 正式依赖可达性：从**隔离 Gateway 的同一网络空间**验证，而不是在宿主上 curl。
# 真实请求之前的门；合成模式没有生产 Redis，如实报不可达，不据此判 PASS。
deps() {
  say "== 会话依赖（宿主回环 → 仅 Gateway 可见的网络） =="
  local rc=0
  # 用**实际的只读配置**认证后 PING：生产会话库启用密码时，未认证 PING 会被拒，
  # 那是探针的错误，不是通路的错误。实现只有一份（tools/verification/m2_live/redis_probe.py），
  # 镜像里与宿主上跑的是同一段代码。
  if [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    docker exec -i m2live03-gateway python /opt/aiops-src/tools/verification/m2_live/redis_probe.py || rc=1
  else
    # 正式模式：目标固定为仅 Gateway 可见的中继（宿主回环 Redis）。
    docker exec -i -e AIOPS_REDIS_HOST=gateway-host-relay \
      m2live03-gateway python /opt/aiops-src/tools/verification/m2_live/redis_probe.py || rc=1
  fi
  say "== 证书与运行中的容器是否同步 =="
  # 重新生成证书后，运行中的 Gateway 仍握着旧信任库、平台仍加载着旧证书：
  # 那一刻文件全都对，握手却全失败。这里比的是**运行中**的两侧。
  docker exec -i -e "M2LIVE_PLATFORM_URL=${M2LIVE_PLATFORM_URL:-https://platform:8900}" \
    m2live03-gateway python /opt/aiops-src/tools/verification/m2_live/cert_probe.py \
      --platform-url "${M2LIVE_PLATFORM_URL:-https://platform:8900}" || rc=1
  say "== 平台（真 HTTPS，校验本栈 CA） =="
  docker exec -i -e "M2LIVE_PLATFORM_URL=${M2LIVE_PLATFORM_URL:-https://platform:8900}" \
    m2live03-gateway python - <<'PY' || rc=1
import json, os, sys, urllib.request
try:
    body = json.loads(urllib.request.urlopen(
        os.environ["M2LIVE_PLATFORM_URL"].rstrip("/") + "/snail-ai/actuator/health", timeout=5).read())
except Exception as exc:  # noqa: BLE001 - 只报类型，不转述正文
    print(f"  FAIL platform: {type(exc).__name__}")
    sys.exit(1)
ok = body.get("status") == "UP"
print(f"  {'ok  ' if ok else 'FAIL'} platform ({body.get('status')})")
sys.exit(0 if ok else 1)
PY
  if [ "${M2LIVE_SYNTH_MODE:-0}" = "1" ]; then
    say "  SYNTHETIC 会话依赖未接生产（本机 Redis 只绑宿主回环）—— 本条在 41 上才是真判据"
    say "  SYNTHETIC 公司身份接口未验证"
  else
    say "== 公司身份接口（从隔离 Gateway 发起，不以宿主连通代替） =="
    docker exec -i m2live03-gateway python /opt/aiops-src/tools/verification/m2_live/upms_probe.py || rc=1
  fi
  return $rc
}

status() {
  "${COMPOSE[@]}" ps
  docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $(containers) 2>/dev/null
}

# ---------------------------------------------------------------- 停栈与删除
# 两条硬规则：
#   1. 停栈失败就不删数据，并且返回非零（旧实现 `rm -rf` 在 docker 失败后照样执行）。
#   2. 删除前必须证明「这是本栈的状态根」：精确匹配、不是符号链接、不是根、不落在
#      生产或任何更上层目录、且目录里没有不是本栈创建的条目。

purge_targets() { printf '%s\n' "$STATE/store" "$STATE/gateway" "$STATE/node" "$STATE/platform"; }

refuse() { red "  拒绝：$*"; return 3; }

verify_purge_target() {
  local real
  if [ -L "$STATE" ]; then
    refuse "$STATE 是符号链接"
    return 3
  fi
  if [ ! -d "$STATE" ]; then
    refuse "$STATE 不存在或不是目录"
    return 3
  fi
  real=$(readlink -f "$STATE")
  case "$real" in
    /|/etc|/usr|/var|/var/lib|/home|/opt|/root|/tmp|/boot|/srv)
      refuse "状态根过于上层: $real"
      return 3 ;;
    /etc/aiops-41*|/var/lib/aiops-41*|/opt/aiops-41*|/etc/*|/usr/*|/boot/*)
      refuse "状态根落在系统/生产目录: $real"
      return 3 ;;
  esac
  # 追加的生产前缀（M2LIVE_FORBIDDEN）与隔离门禁共用同一份语义。
  local prefix
  if [ -n "${M2LIVE_FORBIDDEN:-}" ]; then
    local IFS_SAVE=$IFS
    IFS=:
    for prefix in $M2LIVE_FORBIDDEN; do
      IFS=$IFS_SAVE
      [ -n "$prefix" ] || continue
      case "$real" in
        "$(readlink -f "$prefix")"|"$(readlink -f "$prefix")"/*)
          refuse "状态根落在生产前缀之下: $real"
          return 3 ;;
      esac
    done
    IFS=$IFS_SAVE
  fi
  # 所有权标记：上一次 up 写下的项目名必须与本次一致。
  local marker="$real/.owner"
  if [ ! -f "$marker" ]; then
    refuse "状态根没有所有权标记 $marker（不是本栈创建的，不删）"
    return 3
  fi
  if [ "$(cat "$marker")" != "$PROJECT" ]; then
    refuse "状态根属于另一个项目: $(cat "$marker")"
    return 3
  fi
  local entry name
  for entry in "$real"/*; do
    [ -e "$entry" ] || continue
    name=$(basename "$entry")
    case "$name" in
      store|gateway|node|platform|.owner) ;;
      *) refuse "状态根里有非本栈条目: $name"; return 3 ;;
    esac
  done
  return 0
}

down() {
  local purge=0
  [ "${1:-}" = "--purge" ] && purge=1
  if [ $purge -eq 1 ]; then
    verify_purge_target
    local target_rc=$?
    [ $target_rc -eq 0 ] || return $target_rc
  fi
  "${COMPOSE[@]}" down --remove-orphans
  local rc=$?
  if [ $rc -ne 0 ]; then
    red "  停栈失败（退出码 $rc）：不删除任何数据"
    return $rc
  fi
  if [ $purge -eq 1 ]; then
    local target resolved_root
    resolved_root=$(readlink -f "$STATE")
    while read -r target; do
      [ -e "$target" ] || [ -L "$target" ] || continue
      # 再核对一次每个目标：必须是状态根的**直接子目录**且不是符号链接。
      [ -L "$target" ] && { red "  跳过符号链接 $target"; return 3; }
      [ "$(dirname -- "$(readlink -f "$target")")" = "$resolved_root" ] \
        || { red "  拒绝删除 $target（不是状态根的直接子目录）"; return 3; }
      # 删不掉必须报错：`rm -rf` 在属主不是当前用户时会失败，而「打印已删除、
      # 返回 0」正是本轮修的那类谎报。
      if ! rm -rf -- "$target"; then
        red "  删除失败：$target（多半是属主不对；用宿主上的服务 UID 或维护窗口处理）"
        return 1
      fi
    done < <(purge_targets)
    rm -f -- "$STATE/.owner"
    red "  已删除 $STATE 下的栈数据（不可恢复）"
  else
    say "  数据保留在 $STATE（要删用 down --purge）"
  fi
}

claim_state_root() {
  # 在**预检通过之后**认领状态根。三个前提，任一不成立就拒绝（返回 3），
  # 并且**不留下任何标记**：
  #   - 状态根不是一个符号链接；
  #   - 已有的 `.owner` 要么属于本项目，要么不存在；
  #   - 状态根里没有既不属于本栈、也不是本栈目录的条目。
  # 写入用「临时文件 + mv」，所以中途失败不会留下半个标记。
  local real existing
  [ -L "$STATE" ] && { refuse "状态根是符号链接: $STATE"; return 3; }
  mkdir -p "$STATE" || { red "  建不了状态根: $STATE"; return 1; }
  real=$(readlink -f "$STATE")
  case "$real" in
    /|/etc|/usr|/var|/var/lib|/home|/opt|/root|/tmp|/boot|/srv)
      refuse "状态根过于上层: $real"; return 3 ;;
    /etc/aiops-41*|/var/lib/aiops-41*|/opt/aiops-41*|/etc/*|/usr/*|/boot/*)
      refuse "状态根落在系统/生产目录: $real"; return 3 ;;
  esac
  existing="$real/.owner"
  if [ -f "$existing" ] && [ "$(cat "$existing")" != "$PROJECT" ]; then
    refuse "状态根属于另一个项目（$(cat "$existing")），不改动它"
    return 3
  fi
  local entry name
  for entry in "$real"/*; do
    [ -e "$entry" ] || continue
    name=$(basename "$entry")
    case "$name" in
      store|gateway|node|platform|.owner) ;;
      *) refuse "状态根里有非本栈条目: $name"; return 3 ;;
    esac
  done
  local staged="$real/.owner.tmp.$$"
  printf '%s\n' "$PROJECT" > "$staged" || { red "  写不了状态根"; return 1; }
  mv -f "$staged" "$existing" || { rm -f "$staged"; red "  标记落位失败"; return 1; }
  return 0
}

case ${1:-} in
  preflight) preflight ;;
  up) up ;;
  ready) ready ;;
  verify) verify ;;
  deps) deps ;;
  status) status ;;
  down) shift; down "${1:-}" ;;
  *) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
