#!/usr/bin/env bash
# M3-NODE-GUARD-01 合成验收：单命令复跑。
#
# 三件事，缺一不可：
#   1. 用锁定 JDK 镜像里的 javac 编译**本仓节点源码 + 本目录验收代码**（不安装任何系统软件）；
#   2. 无网络容器里跑启用守卫的整组场景；
#   3. 同一夹具上跑反向控制（--no-manifest）：同一改动必须真的到达合成上游。
#
# 依赖由参数给出，仓库里不写任何私有绝对路径：
#   M3NODE_JDK_IMAGE   锁定 JDK 镜像（含 sha256 的 ref）
#   M3NODE_DEPS        锁定依赖目录（其中 lib/ 放锁定 jar）
# 缺任一依赖即非零退出，**不跳过、不当作通过**。
#
# 失败传播（本脚本在这里错过一次，回归钉住）：
#   验收进程的退出码必须**逐级传上来**。`java … | tee log` 的退出码是 `tee` 的 0，
#   于是 Java 故意失败 17 时顶层仍打印成功、退出 0 —— 「管道掩盖验证结果」。
#   现在：不把 java 放进管道，容器内 `set -o pipefail`，每步显式查退出码，
#   且**先**确认日志里出现 `STATUS=PASS` 才算该步通过；任一步失败立即停并打印 FAILURE。
#
# 自检（覆盖 guarded 与 reverse 两条路径）：
#   M3NODE_SELFTEST=guarded|reverse|both
#   该模式下对应阶段改跑一个**故意失败**（exit 17）的类。期望：顶层非零退出，
#   且**不**打印整体成功。自检自身失败（比如失败竟然没传上来）时本脚本退出 1。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
JDK_IMAGE="${M3NODE_JDK_IMAGE:-}"
DEPS="${M3NODE_DEPS:-}"
OUT="${M3NODE_OUT:-$HERE/build}"
SELFTEST="${M3NODE_SELFTEST:-}"
# 让真实阶段故意失败：guarded / reverse / both。自检用它证明失败确实传到顶层。
FORCE_FAILURE="${M3NODE_FORCE_PHASE_FAILURE:-}"

if [[ -z "$JDK_IMAGE" ]]; then
  echo "missing M3NODE_JDK_IMAGE (e.g. eclipse-temurin:21-jdk@sha256:...)" >&2
  exit 2
fi
if [[ -z "$DEPS" || ! -d "$DEPS/lib" ]]; then
  echo "missing M3NODE_DEPS with lib/ (locked jars)" >&2
  exit 2
fi

mkdir -p "$OUT/classes"
echo "== compiling node sources + acceptance code"
docker run --rm --pull=never --network=none --cap-drop=ALL \
  --user "$(id -u):$(id -g)" \
  -v "$REPO:/repo:ro" -v "$DEPS:/deps:ro" -v "$OUT:/out" "$JDK_IMAGE" bash -c '
    set -euo pipefail
    find /repo/services/execution-node/src/main/java -name "*.java" > /tmp/srcs.txt
    javac -nowarn -proc:none -cp "/deps/lib/*" -d /out/classes \
      @/tmp/srcs.txt /repo/tools/experiments/m2_close/m3node/*.java
  '

if [[ -n "$SELFTEST" || -n "$FORCE_FAILURE" ]]; then
  # 包名必须与启动类名一致（`m3node.DeliberateFailure`）—— 否则会以 ClassNotFoundException
  # 退出 1，被当成"失败传上来了"，而那根本不是我们注入的那次失败。
  mkdir -p "$OUT/selftest-src/m3node"
  cat > "$OUT/selftest-src/m3node/DeliberateFailure.java" <<'JAVA'
package m3node;

/** 自检用：故意以 17 退出，并留下可判定的标记（编译失败/类找不到都不算通过）。 */
public class DeliberateFailure {
    public static void main(String[] args) {
        System.out.println("DELIBERATE_FAILURE_SENTINEL");
        System.out.println("STATUS=FAIL intentional exit 17");
        System.exit(17);
    }
}
JAVA
  docker run --rm --pull=never --network=none --cap-drop=ALL \
    --user "$(id -u):$(id -g)" -v "$DEPS:/deps:ro" -v "$OUT:/out" "$JDK_IMAGE" bash -c '
      set -euo pipefail
      javac -d /out/classes /out/selftest-src/m3node/DeliberateFailure.java
    '
fi

# 跑一个阶段：失败立即停，且必须看到 STATUS=PASS。绝不把非零退出码吞进管道。
run_phase() {
  local label="$1" log="$2"; shift 2
  echo "== $label"
  local rc=0
  docker run --rm --pull=never --network=none --cap-drop=ALL \
    --user "$(id -u):$(id -g)" \
    -v "$DEPS:/deps:ro" -v "$OUT:/out" "$JDK_IMAGE" bash -c "$*" >"$log" 2>&1 || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "FAILURE: $label exited non-zero (rc=$rc)"
    tail -20 "$log" 2>/dev/null || true
    echo "STATUS=FAIL"
    exit 1
  fi
  if [[ ! -s "$log" ]]; then
    echo "FAILURE: $label produced no log"
    echo "STATUS=FAIL"
    exit 1
  fi
  if ! grep -qE "^STATUS=PASS$" "$log"; then
    echo "FAILURE: $label did not report STATUS=PASS" >&2
    tail -20 "$log" >&2 || true
    echo "STATUS=FAIL" >&2
    exit 1
  fi
  grep -E "^(PASS|FAIL|RED-AS-EXPECTED)|failures=|STATUS=" "$log" | tail -5
}

# 阶段入口。`M3NODE_FORCE_PHASE_FAILURE` 会让**这个真实阶段**改跑故意失败的类 ——
# 自检走的就是这条路径，而不是另写一套 docker 检查。
phase_main() {
  local phase="$1"
  if [[ "$FORCE_FAILURE" == "$phase" || "$FORCE_FAILURE" == "both" ]]; then
    echo "DeliberateFailure"
  else
    echo "NodeGuardAcceptance"
  fi
}

guarded_phase() {
  local main; main="$(phase_main guarded)"
  run_phase "$1" "$2" \
    "set -euo pipefail
     java -cp \"/out/classes:/deps/lib/*\" m3node.$main"
}

reverse_phase() {
  local main; main="$(phase_main reverse)"
  local flag=""; [[ "$main" == "NodeGuardAcceptance" ]] && flag=" --no-manifest"
  run_phase "$1" "$2" \
    "set -euo pipefail
     java -cp \"/out/classes:/deps/lib/*\" m3node.$main$flag"
}

# 自检：**递归调用本脚本**跑真实阶段，并注入故意失败。
# 关键：走的就是 guarded_phase / reverse_phase / run_phase 这条真实路径，
# 而不是另写一套 docker 检查 —— 否则把 run_phase 改成 return 0 自检照样"通过"。
selftest_phase_propagates() {
  local phase="$1" log="$OUT/self-test-$1.log" rc=0
  M3NODE_SELFTEST= M3NODE_FORCE_PHASE_FAILURE="$phase" \
    bash "$HERE/run.sh" >"$log" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    return 1
  fi
  # 条件一：**故意失败程序自身的标记**确实出现。只看"退出码非零"远远不够 ——
  # 曾出现两种假通过：内层因后面的 sha256sum 找不到日志而偶然非零；以及
  # DeliberateFailure 无包名导致 ClassNotFoundException（exit 1，而不是注入的 17）。
  if ! grep -qF "DELIBERATE_FAILURE_SENTINEL" "$log"; then
    return 1
  fi
  # 条件二：Java 确实以 17 退出（run_phase 会把真实退出码打印出来）。
  if ! grep -qE "FAILURE: .* exited non-zero \(rc=17\)" "$log"; then
    return 1
  fi
  # 条件三：对应阶段与顶层都非零，且**不**以整体成功收尾。
  # 看**最后一行**状态，而不是 grep 全文（各阶段成功时会打印自己的 STATUS=PASS）。
  local last
  last="$(grep -E "^STATUS=" "$log" | tail -1)"
  if [[ "$last" == "STATUS=PASS" ]]; then
    return 1
  fi
  return 0
}

if [[ -n "$SELFTEST" ]]; then
  # 期望两条**真实路径**都把故意失败传到顶层。任一条返回成功或仍打印整体成功，
  # 就是自检失败（脚本回归了「管道吞错」）。
  for phase in guarded reverse; do
    echo "== self-test: forced failure in the real $phase phase must reach the top level"
    if selftest_phase_propagates "$phase"; then
      echo "   propagated as expected ($phase)"
    else
      echo "FAILURE: $phase phase did not propagate a non-zero exit to the top level" >&2
      echo "STATUS=FAIL" >&2
      exit 1
    fi
  done
  echo "SELFTEST=PASS (both real phases propagate failures)"
  exit 0
fi

guarded_phase "guarded acceptance" "$OUT/guarded.log"
reverse_phase "reverse control (same fixtures, guard not enabled)" "$OUT/counter-proof.log"

echo "== artifacts"
sha256sum "$OUT/guarded.log" "$OUT/counter-proof.log" | tee "$OUT/logs.sha256"
echo "STATUS=PASS"
