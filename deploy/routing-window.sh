#!/usr/bin/env bash
# 路由判定窗口取数（#405 的观察期判据，#464 的观测面）。
#
# 用法： deploy/routing-window.sh [窗口起点 ISO8601]
#
# 不传起点时，用**当前进程的启动时刻**作为窗口起点 —— 那是「本次部署之后」的
# 自然边界，也是 #464 部署后第一次取数用的那一版。
#
# 它回答的是 #405 到期评估真正需要的那两个数，而不是更容易取到的那个：
#   · 窗口内 route_type='routing' 的**成功**计数（#464 补上的那一侧）
#   · 窗口内 route_type='routing' 的**失败**计数与错误码分布
# 两侧都为 0 ⇒ 窗口内**没有流量**，结论是「判定路径未被使用」，不是「连续运行通过」。
#
# 只读：不写库、不重启服务、不读用户文本（只取计数与时间戳）。
set -euo pipefail

HOST=${AIOPS_41_HOST:-aiops-41}
DB=${AIOPS_41_DB:-/var/lib/aiops-41/gateway/gateway.db}
PY=${AIOPS_41_PY:-/opt/aiops-41/.venv/bin/python}
SERVICE=${AIOPS_41_SERVICE:-aiops-gateway-41}

if [ $# -ge 1 ]; then
  WINDOW_START=$1
else
  # The process's start time, in UTC. `ps -o lstart=` prints a LOCAL wall clock
  # with no zone, and `date -d` reads it back in the host's zone — which on this
  # host is CST, so the "conversion" returned 02:39+00:00 for a process that
  # started at 18:39Z. That silently moved the window eight hours EARLIER, which
  # is the direction that invents traffic rather than hiding it.
  # `/proc/<pid>` carries the epoch, so this reads the clock the rows were
  # written with and needs no zone arithmetic at all.
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
if completed == 0 and failed == 0:
    print("\n判定：窗口内没有流量 —— 结论是「判定路径未被使用」，不是「连续运行通过」。")
elif completed == 0:
    print("\n判定：窗口内有失败但没有成功 —— 判定路径有问题，不是「没流量」。")
else:
    print(f"\n判定：成功 {completed} / 失败 {failed} —— 两侧都在，比率可算。")
PY
