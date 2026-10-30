# 网关不能先于节点上线（CD 闸门 + 源码摘要）

分支 `feat/node-artifact-gate`，基线 `425b5f79`。

## 背景

2026-10-30 16:20 CST，CD 把网关自动升到 #1210，节点停在 #1189 之前，管家侧工具全部
`403 OPERATOR_TOOL_CONTRACT_OUTDATED`。止血（切节点到同修订）已在 #1213 记录，本片做根治。

## 已完成

**① 节点制品闸门**（`deploy/deploy-41.sh`）：部署网关前，取目标 commit 祖先里**最近一次**
改 `services/execution-node/` 的 commit，要求 41 上实际在跑的节点（读它自己的
`AIOPS_NODE_BUILD`）等于它或其后代，否则停止并说明怎么修。

判据是「最近一次改节点」而不是「本次 commit 改没改节点」—— 后者会漏掉跨 commit 的情形
（A 改节点、B 只改 src）。

**② 源码摘要入锁**（`images.lock.json` 的 `node.source_sha256` + `deploy/node-runtime/source_digest.py`）：
源码变了而记录没变 ⇒ CI 红。CI 不构建 Java，所以 jar 摘要只能在部署时验；源码摘要不需要
JDK，正好补上「忘了重建」这个空档。

顺带把 lock 里四个死值更新成事实：`sha256`（止血时部署的 jar）、`bytes`、`source_commit`
（`ae290a0d`）、`source_sha256`；`HOST-CHANGE.md` 的节点行同步。

## 验证

- 全量 **3551 passed / 9 skipped**；ruff 通过。
- `source_digest.py --check` match；`context_digest.py --check` match。
- 新增 4 条测试：闸门的判据来源、`--is-ancestor` 的**方向**（真实 git）、源码摘要与 lock
  一致、源码变了摘要就变（含「target/ 不参与」的对照）。

## 缺口与边界

- **CI 仍然不构建 Java**：本片让「忘了重建」在 CI 里有信号，没有改变这个事实。
- **`--dry-run` 会读 41**：闸门的远端读与既有的依赖漂移检查同一行为，dry-run 的文档
  写着「不碰 41」——这处不一致是既有的，本片没有让它变差，也没有顺手修。
- **未完成业务验收**：本片是流程判据，不产出业务结论。
