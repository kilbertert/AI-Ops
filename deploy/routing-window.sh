#!/usr/bin/env bash
# 路由判定窗口取数（#405 的观察期判据，#464 的观测面）。
#
# 用法： deploy/routing-window.sh [窗口起点，形如 2026-10-01T02:00:00+00:00]
#
# 不传起点时，用**当前网关进程的启动时刻**作为窗口起点 —— 那是「本次部署之后」的
# 自然边界，也是 #464 部署后第一次取数用的那一版。
#
# 它回答的是 #405 到期评估真正需要的那两个数，而不是更容易取到的那个：
#   · 窗口内 route_type='routing' 的**成功**计数（#464 补上的那一侧）
#   · 窗口内 route_type='routing' 的**失败**计数与错误码分布
# 两侧都为 0 ⇒ 结论是「判定路径未被使用」，不是「连续运行通过」。
#
# 只读：不写库、不重启服务、不读用户文本（只取计数与时间戳）。
set -euo pipefail

HOST=${AIOPS_41_HOST:-aiops-41}
DB=${AIOPS_41_DB:-/var/lib/aiops-41/gateway/gateway.db}
PY=${AIOPS_41_PY:-/opt/aiops-41/.venv/bin/python}
SERVICE=${AIOPS_41_SERVICE:-aiops-gateway-41}

# 这四个值都会被嵌进远端命令，所以逐个按形状校验。它们平时都取默认值，
# 校验的成本是零；不校验的成本是「一个只读脚本能被一个环境变量改成任意远端命令」。
require_shape() {
  local name=$1 value=$2 pattern=$3
  # 换行必须先单独拒掉：`grep -E` 是**逐行**匹配的，一个「合法首行 + 换行 + 命令」
  # 的值会在正则那一关通过，而远端 shell 会执行第二行。pattern 里的 `$` 拦不住它。
  case "$value" in
    *$'\n'* | *$'\r'*)
      printf '环境变量 %s 含换行，拒绝：%s\n' "$name" "$value" >&2
      exit 2
      ;;
  esac
  if ! printf '%s' "$value" | grep -Eq "$pattern"; then
    printf '环境变量 %s 的形状不被接受：%s\n' "$name" "$value" >&2
    exit 2
  fi
}
require_shape AIOPS_41_HOST "$HOST" '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
require_shape AIOPS_41_SERVICE "$SERVICE" '^[A-Za-z0-9][A-Za-z0-9@._-]{0,63}$'
require_shape AIOPS_41_DB "$DB" '^/[A-Za-z0-9._/-]{1,200}$'
require_shape AIOPS_41_PY "$PY" '^/[A-Za-z0-9._/-]{1,200}$'

#: The only shape this script accepts for a window start. It is not a stylistic
#: check: the value travels into a remote `sh -c` string, and it is compared as
#: TEXT against `created_at`, which the store writes as UTC ISO8601. Both
#: concerns are answered by accepting exactly one canonical form.
WINDOW_PATTERN='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$'

normalize_window() {
  local value=$1
  if ! printf '%s' "$value" | grep -Eq "$WINDOW_PATTERN"; then
    cat >&2 <<EOF
窗口起点必须是 UTC 的 ISO8601，形如 2026-10-01T02:00:00+00:00；收到：$value

为什么只收这一种形状：
  · 它会被嵌进远端命令，含引号或元字符的值会改变那条命令（本脚本只读，但没有理由给它开口子）；
  · 它按**文本**与 created_at 比较，而后者是 UTC ISO8601 文本。带 +08:00 偏移的起点
    字符串比较会得到错误的边界（10:00+08:00 应等于 02:00Z，字符串里却比 03:00Z 大）。
需换算时先跑：date -u -d '<你的时间>' +%Y-%m-%dT%H:%M:%S+00:00
EOF
    exit 2
  fi
  # 形状对了不等于时刻存在：`2026-13-01T02:00:00+00:00` 通过了上面的正则，
  # 而它按文本比较会把窗口算错（不是报错，是**静默地**变窄）。让 date 真正解析一次。
  if ! date -u -d "$value" +%Y-%m-%dT%H:%M:%S+00:00 >/dev/null 2>&1; then
    printf '窗口起点不是有效的时刻：%s\n' "$value" >&2
    exit 2
  fi
  printf '%s' "$value"
}

if [ $# -ge 1 ]; then
  WINDOW_START=$(normalize_window "$1")
else
  # 取进程启动时刻的 UTC 值。踩过一次，记在这里：`ps -o lstart=` 给的是**本地墙钟
  # 且不带时区**，`date -d` 又按主机时区读它 —— 本主机是 CST，于是「换算」把 18:39Z
  # 的进程算成了 02:39+00:00，窗口被悄悄**前移 8 小时**。这个方向会凭空造出流量，
  # 比藏起流量更危险。`/proc/<pid>` 里是 epoch，读它就不需要任何时区算术。
  # shellcheck disable=SC2029  # the expansion is meant to happen here: the
  # remote side runs the remote shell's own commands on the remote clock.
  WINDOW_START=$(ssh "$HOST" "date -u -d @\$(stat -c %Y /proc/\$(systemctl show $SERVICE -p MainPID --value)) +%Y-%m-%dT%H:%M:%S+00:00")
  echo "窗口起点（当前进程启动，UTC）：$WINDOW_START"
fi

# shellcheck disable=SC2029  # sending these values to the remote side IS the point
ssh "$HOST" "sudo -u aiops41 $PY - '$DB' '$WINDOW_START'" <<'PY'
import sqlite3
import sys

db, start = sys.argv[1], sys.argv[2]
connection = sqlite3.connect(db)
routing = connection.execute(
    "SELECT outcome, COALESCE(error_code, '-'), COUNT(*) FROM agent_run_metrics"
    " WHERE route_type = 'routing' AND created_at > ? GROUP BY 1, 2",
    (start,),
).fetchall()
by_route = connection.execute(
    "SELECT route_type, COUNT(*) FROM agent_run_metrics WHERE created_at > ? GROUP BY 1",
    (start,),
).fetchall()
latest = connection.execute(
    "SELECT created_at, route_type, outcome FROM agent_run_metrics ORDER BY created_at DESC LIMIT 1"
).fetchone()
all_routing = connection.execute(
    "SELECT outcome, COALESCE(error_code, '-'), COUNT(*) FROM agent_run_metrics"
    " WHERE route_type = 'routing' GROUP BY 1, 2"
).fetchall()

print(f"窗口内 routing 行：{routing}")
print(f"窗口内任何路由的行：{by_route}")
print(f"全表最近一条：{latest}")
print(f"历史 routing 行（全部，供对照）：{all_routing}")

completed = sum(n for outcome, _, n in routing if outcome == "completed")
failed = sum(n for outcome, _, n in routing if outcome == "failed")
other_traffic = sum(n for route_type, n in by_route if route_type != "routing")
if completed == 0 and failed == 0:
    if other_traffic == 0:
        # Not "the gateway served nothing": `/health` and the media routes write
        # no metric rows, so an empty window is consistent with traffic that
        # simply does not reach this table. Say what was observed, not more.
        print("\n判定：窗口内没有任何指标行 —— 据此无法判断网关是否处理过请求。")
    else:
        # The two are different facts and the difference matters: "nobody asked
        # anything" is not the same as "people asked and the decision path was
        # never consulted". Collapsing them would report the second as the
        # first, which is the same shape of error this whole ticket is about.
        print(
            f"\n判定：窗口内有其它路径的请求（{other_traffic} 条），但判定路径**一次都没被使用** ——"
            " 结论是「判定路径未被使用」，不是「连续运行通过」。"
        )
elif completed == 0:
    print("\n判定：窗口内有失败但没有成功 —— 判定路径有问题，不是「没流量」。")
else:
    print(f"\n判定：成功 {completed} / 失败 {failed} —— 两侧都在，比率可算。")
PY
