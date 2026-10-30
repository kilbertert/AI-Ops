#!/usr/bin/env bash
# M3-NODE-LINK-R1：同 JVM 并发验收的单命令入口。
#
# 为什么单独一个脚本：`verify_node_link.py` 证明的是「真 Gateway + 真节点」的端到端，
# 每次派发只起一个节点进程，结构上覆盖不到「同一个 JVM 里两个 run」。把并发判据塞进去
# 就得伪造 run 记录或加第二个编排路径 —— 两件事都不该发生在端到端验收里。
# 这里用 `ConcurrentRunAcceptance`（自带只回答领取/完成的替身 Gateway），四个模式：
#
#   distinct     两个不同 run 并发：都完成，各领一次、各一条通知
#   early        相同 run 重入，B 在 A 构建 ChatClient 之前进入 → B 本地被拒
#   late         相同 run 重入，A 已领取、末轮请求还挂在上游 → B 本地被拒
#   first-error  首轮上游失败：无领取，只发一次 failed
#
# 依赖由参数给出，仓库里不写私有绝对路径：
#   M3NODE_JDK_IMAGE   锁定 JDK 镜像（含 sha256 的 ref）
#   M3NODE_DEPS        锁定依赖目录（其中 lib/ 放锁定 jar）
# 缺任一依赖即非零退出，**不跳过、不当作通过**。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../../.." && pwd)"
JDK_IMAGE="${M3NODE_JDK_IMAGE:-}"
DEPS="${M3NODE_DEPS:-}"
OUT="${M3NODE_OUT:-$HERE/build}"

if [[ -z "$JDK_IMAGE" ]]; then
  echo "missing M3NODE_JDK_IMAGE (e.g. eclipse-temurin:21-jdk@sha256:...)" >&2
  exit 2
fi
if [[ -z "$DEPS" || ! -d "$DEPS/lib" ]]; then
  echo "missing M3NODE_DEPS with lib/ (locked jars)" >&2
  exit 2
fi

mkdir -p "$OUT/classes"
echo "== compiling node sources + concurrency acceptance"
docker run --rm --pull=never --network=none --cap-drop=ALL \
  --user "$(id -u):$(id -g)" \
  -v "$REPO:/repo:ro" -v "$DEPS:/deps:ro" -v "$OUT:/out" "$JDK_IMAGE" bash -c '
    set -euo pipefail
    find /repo/services/execution-node/src/main/java -name "*.java" > /tmp/srcs.txt
    javac -nowarn -proc:none -cp "/deps/lib/*" -d /out/classes \
      @/tmp/srcs.txt /repo/tools/experiments/m2_close/m3node/m3link/ConcurrentRunAcceptance.java
  '

failed=0
for mode in distinct early late first-error; do
  echo "== $mode"
  # `--network=host`：节点必须打宿主 loopback 上的替身 Gateway。
  # 这是本机验收的已知取舍，不是隔离证明。
  if ! docker run --rm --pull=never --network=host --cap-drop=ALL \
      --user "$(id -u):$(id -g)" \
      -v "$OUT/classes:/out/classes:ro" -v "$DEPS:/deps:ro" "$JDK_IMAGE" \
      bash -c "java -cp '/out/classes:/deps/lib/*' m3link.ConcurrentRunAcceptance $mode" \
      >"$OUT/$mode.log" 2>&1; then
    echo "FAILURE: $mode exited non-zero"
    tail -20 "$OUT/$mode.log" || true
    failed=1
  fi
  grep -E "^(PASS|FAIL|OBSERVED)|STATUS=" "$OUT/$mode.log" || true
done

if [[ "$failed" -ne 0 ]]; then
  echo "STATUS=FAIL"
  exit 1
fi
sha256sum "$OUT"/*.log > "$OUT/logs.sha256"
echo "STATUS=PASS"
