## 2026-10-23 · 修复 `grep -q` + `pipefail` 的判据缺陷（#1041）

**目标/分支**：`fix/classifier-flake-diagnosable`，基线 `bd8e0a6`。

### 一、查清了一个「重跑就过」的失败

`verify` 间歇性失败在 `test_rollback_classifier_branches`。本地 40 次全过，
同一 SHA 重跑即过——形状与「代码有 bug」一样，但代码是对的。

根因：`set -o pipefail` + `printf | grep -q`。`grep -q` 首个命中即退出 → 生产者收到
SIGPIPE → `pipefail` 取到 141 → `if` 把命中判成未命中。**只有命中点之后还有大量数据
才触发**，这是它小 fixture 复现不出来、CI 上随机的全部原因。

### 二、影响面（两处是生产代码）

| 位置 | 实测 |
|---|---|
| `classify-remote-result.sh` | 20 万行输入时把「回滚也失败」误报成「状态未知」 |
| `deploy-41.sh` 的 `/health` 断言 | 10 万行时 30/30 误判 |
| 测试层判据 | 会把 PASS 读成 FAIL |

生产那两处**误报方向是最坏的**：严重故障被降级成「不确定」。

### 三、改法

`grep -q` → `grep -c … >/dev/null`。新增回归用例，撤回修复即转红，失败文本与当初 CI 逐字一致。

### 四、边界

`dify-36/check.sh` 与 `make-certs.sh` 是同型写法但**未改**：前者输入极小，后者用 `-qx`。
将来输入变大时会以同样方式失败，ADR 0114 的作用是让那一次能被立刻认出来。

测试层那处改成 `-c` 属防御性，**没有测试钉住**（实测改回仍全绿），已在 ADR 里标注。
