#!/usr/bin/env bash
# 本栈的零网络自检（#579）。
#
# 不依赖 36、不拉镜像、不起容器 —— 只检查这份 compose 与 squid 配置的**形状**。
# 跑法：  deploy/dify-36/check.sh
#
# 覆盖两件会静默出错的事：
#   1. 发布的端口集合。多一个口就是多一个对外面；绑定地址写错（127.0.0.1 vs 0.0.0.0）
#      在 `docker compose config` 里看得出来，但没人天天看。
#   2. **squid 白名单真的落在私有网段 deny 之前**。#579 选的处置是"保留 SSRF 过滤 +
#      加白名单"，而这条路有一个沉默的失败态：`http_access allow` 若排在
#      `http_access deny to_private_networks` 之后，白名单就是一条不生效的规则 ——
#      看起来配了，实际全拒。第 2 项就是钉住这个顺序。
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok: $*"; }

# ---------------------------------------------------------------- 1. compose
command -v docker >/dev/null || fail "need docker to validate the compose file"

# 在**临时副本**里跑 compose 校验：服务的 `env_file: ./.env` 要求该文件存在，
# 而这个自检不该在仓库里留下一个 .env。
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
cp -r docker-compose.yaml nginx ssrf_proxy "$tmp_dir/"
tmp_env="$tmp_dir/.env"
# 只为通过 compose 的变量插值；**不写任何真实凭据**。
{
  echo "DIFY_REGISTRY=docker.1panel.live"
  echo "DIFY_EXPOSE_PORT=10008"
  echo "SECRET_KEY=$(printf 'x%.0s' {1..43})"
  echo "INIT_PASSWORD=$(printf 'x%.0s' {1..16})"
  echo "DB_PASSWORD=$(printf 'x%.0s' {1..16})"
  echo "REDIS_PASSWORD=$(printf 'x%.0s' {1..16})"
  echo "PLUGIN_DAEMON_KEY=$(printf 'x%.0s' {1..43})"
  echo "PLUGIN_DIFY_INNER_API_KEY=$(printf 'x%.0s' {1..43})"
  echo "CELERY_BROKER_URL=redis://:x@redis:6379/1"
} >"$tmp_env"

docker compose -f "$tmp_dir/docker-compose.yaml" --env-file "$tmp_env" config --format json \
  >"$tmp_dir/resolved.json" 2>/dev/null \
  || fail "docker compose config rejected docker-compose.yaml"

python3 - "$tmp_dir/resolved.json" <<'PY' || exit 1
import json, sys

d = json.load(open(sys.argv[1]))
svcs = set(d["services"])

want = {"init_permissions", "api", "worker", "web", "db_postgres", "redis",
        "plugin_daemon", "ssrf_proxy", "nginx"}
if svcs != want:
    raise SystemExit(f"FAIL: 服务集不符\n  多: {sorted(svcs - want)}\n  少: {sorted(want - svcs)}")
print(f"ok: 服务集 = {len(svcs)} 个（PRD #577 的 8 个 + nginx 单一入口）")

# 本实例不接向量库、不接 Dify 自带 agent runtime / sandbox。
for forbidden in ("sandbox", "local_sandbox", "agent_backend", "agent_ssrf_proxy",
                  "worker_beat", "api_websocket", "weaviate", "qdrant", "milvus", "db_mysql"):
    if forbidden in svcs:
        raise SystemExit(f"FAIL: 不该出现的服务在：{forbidden}")

ports = []
for name, s in d["services"].items():
    for p in s.get("ports") or []:
        ports.append((name, p.get("published"), p.get("target"), p.get("host_ip")))
if ports != [("nginx", "10008", 80, "0.0.0.0")]:
    raise SystemExit(f"FAIL: 发布端口集合不符，实际 {ports}；"
                     "本栈只允许 nginx 发布 10008，且必须显式绑 0.0.0.0（已登记为临时暴露）")
print("ok: 唯一发布端口 nginx 0.0.0.0:10008 -> :80")

# 数据库与缓存不得对外发布。
for name in ("db_postgres", "redis", "plugin_daemon", "ssrf_proxy", "api", "worker", "web"):
    if d["services"][name].get("ports"):
        raise SystemExit(f"FAIL: {name} 发布了端口；内部服务不得有 host 发布")
print("ok: 其余服务无 host 端口发布")

# postgres 数据目录的挂载根守卫（README「落地期踩到的五处」第 4 点）。
# 两条合起来才有意义：init_permissions 去补 o+x，且 db_postgres 必须等它跑完。
# 少了任一条，uid-70 的 mkdir 就可能穿不过 770 的挂载根，症状是"服务在跑但
# 连不上库"。这是个只会静默出错的形状，值得钉死。
init_svc = d["services"]["init_permissions"]
init_mounts = {(v.get("source") or "", v.get("target")) for v in (init_svc.get("volumes") or [])}
# 源路径是相对的，compose 已把它解析成绝对路径；这里只比对尾部，免得绑死部署目录。
want_targets = {("/volumes/app/storage", "/app/api/storage"),
                ("/volumes/db/data", "/db-data")}
got = {(s[s.rfind("/volumes/"):] if "/volumes/" in s else s, t) for s, t in init_mounts}
if got != want_targets:
    raise SystemExit(f"FAIL: init_permissions 的挂载不符 {sorted(init_mounts)}；"
                     "它必须同时挂 app/storage 与 db/data（后者用来补挂载根的 o+x）")
db_dep = (d["services"]["db_postgres"].get("depends_on") or {}).get("init_permissions")
if (db_dep or {}).get("condition") != "service_completed_successfully":
    raise SystemExit(f"FAIL: db_postgres 没有等 init_permissions 完成（现在是 {db_dep}）；"
                     "冷启动时 postgres 的 uid-70 mkdir 会与那条 chmod 抢跑")
print("ok: init_permissions 挂 db/data 且 db_postgres 等它完成（uid-70 遍历守卫在位）")

# storage 属主不变式：init_permissions 必须**无条件** chown 1001:1001（不能靠
# 卷内的 flag 文件幂等 —— flag 会锁住"已初始化"，而 api 以 uid 1001 跑）。
init_cmd = " ".join(init_svc.get("command") or [])
if "chown -R 1001:1001 /app/api/storage" not in init_cmd:
    raise SystemExit("FAIL: init_permissions 不再 chown /app/api/storage")
if "rm -f /app/api/storage/.init_permissions" not in init_cmd:
    raise SystemExit("FAIL: init_permissions 没有清掉卷内 flag；"
                     "flag 幂等会让 chown 被历史锁死，api 写 privkeys/ 时 Permission denied")
print("ok: init_permissions 每次 up 无条件 chown storage 并清掉卷内 flag")

# api/worker 拨 plugin_daemon 的地址。第 3 类"只会静默出错"的形状：留空不报错、
# 探活 200，只有真用到插件的路径（导出 DSL 查插件安装）才 Errno 111 —— 因为 api 侧
# 默认值是 `http://localhost:5002`，那是"5002 发布到宿主"的写法，而本实例不发布内部口。
# 注意它**不是** PLUGIN_REMOTE_INSTALL_HOST（那个是调试口）、也不是
# PLUGIN_DIFY_INNER_API_URL（那是反向）。
for name in ("api", "worker"):
    url = (d["services"][name].get("environment") or {}).get("PLUGIN_DAEMON_URL")
    if url != "http://plugin_daemon:5002":
        raise SystemExit(f"FAIL: {name}.PLUGIN_DAEMON_URL = {url!r}，"
                         "必须是 http://plugin_daemon:5002；留空会退回 api 默认的 "
                         "localhost:5002，插件相关路径 Errno 111 却探活 200")
print("ok: api/worker 拨 plugin_daemon 的地址显式指向 compose 内网名")

# 镜像源必须显式写死（Docker Hub 在 36 不可达）。
for name, s in d["services"].items():
    if not s.get("image", "").startswith("docker.1panel.live/"):
        raise SystemExit(f"FAIL: {name} 的镜像没走已记录的镜像源：{s.get('image')}")
print("ok: 全部镜像经 docker.1panel.live")
PY
ok "compose 形状自检通过"

# ------------------------------------------------------------- 2. squid 顺序
# 复刻 ssrf_proxy/docker-entrypoint.sh 里生成白名单的那段（该文件是上游原样拷来的，
# 逻辑改动只发生在 docker/ssrf_proxy/docker-entrypoint.sh 上游；此处是它的影子，
# 若上游改了而这里没改，第 2 项就会失效 —— 所以顺带断言影子与上游文件的骨架一致）。
ENTRYPOINT=ssrf_proxy/docker-entrypoint.sh
grep -q 'write_optional_private_allowlist' "$ENTRYPOINT" \
  || fail "squid entrypoint 变了（找不到 write_optional_private_allowlist）；自检需要同步"
grep -q 'http_access allow client_localnet' "$ENTRYPOINT" \
  || fail "squid entrypoint 的加白格式变了；自检需要同步"

# 生成结果按上游语义：先 deny to_private_networks，再加白，最后 allow 本地/兜底 deny。
gen() { # $1=ips $2=domains
  local ips="$1" domains="$2"
  echo "# Generated by docker-entrypoint.sh."
  echo "# Allows selected private targets before the default private-network deny rule."
  if [ -n "${ips//[[:space:]]/}" ]; then
    printf 'acl dify_allowed_private_networks dst %s\n' "${ips//,/ }"
    printf 'http_access allow client_localnet %s\n' "dify_allowed_private_networks"
  fi
  if [ -n "${domains//[[:space:]]/}" ]; then
    printf 'acl dify_allowed_private_domains dstdomain %s\n' "${domains//,/ }"
    printf 'http_access allow client_localnet %s\n' "dify_allowed_private_domains"
  fi
}

assert_order() { # $1=generated file
  local gen_allow deny_line
  # squid.conf.template 的行序（deny to_private_networks 在 include 之后）
  grep -q 'http_access deny to_private_networks' ssrf_proxy/squid.conf.template \
    || fail "squid.conf.template 里没有 deny to_private_networks —— 处置前提变了"
  grep -q 'include /etc/squid/dify_allow_private.conf' ssrf_proxy/squid.conf.template \
    || fail "squid.conf.template 不再 include 白名单文件"
  # 关键：模板里 include 必须排在 deny to_private_networks **之前**
  local inc_line deny_line_n
  inc_line=$(grep -n 'include /etc/squid/dify_allow_private.conf' ssrf_proxy/squid.conf.template | cut -d: -f1)
  deny_line_n=$(grep -n 'http_access deny to_private_networks' ssrf_proxy/squid.conf.template | cut -d: -f1)
  [ "$inc_line" -lt "$deny_line_n" ] \
    || fail "白名单 include（行 $inc_line）不在私有网段 deny（行 $deny_line_n）之前 —— 白名单不会生效"
  ok "squid 模板：白名单 include(行 $inc_line) 先于 deny to_private_networks(行 $deny_line_n)"
}

gen "" "" >/dev/null
assert_order /dev/null
# 空值 ⇒ 不产生任何 allow 行（与上游默认逐字一致，过滤全开）
[ -z "$(gen "" "" | grep 'http_access allow' || true)" ] \
  || fail "空白名单却生成了 allow 行"
ok "squid 空白名单 ⇒ 无 allow 行（过滤全开，与上游默认一致）"

# 非空 ⇒ 生成 acl + allow，且格式用的是 dst / dstdomain
out="$(gen '10.1.2.3/32' 'api.example.com')"
echo "$out" | grep -q 'acl dify_allowed_private_networks dst 10.1.2.3/32' \
  || fail "IP 白名单格式不符: $out"
echo "$out" | grep -q 'acl dify_allowed_private_domains dstdomain api.example.com' \
  || fail "域名白名单格式不符: $out"
echo "$out" | grep -q 'http_access allow client_localnet dify_allowed_private_networks' \
  || fail "IP 白名单没有生成放行行"
ok "squid 非空白名单 ⇒ acl+allow 形状正确"

echo
echo "全部通过。"
