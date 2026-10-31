# 真实预览的执行障碍（2026-10-31）

## 已确认事实

目标应用 `394b5399-…`（WF01-Ulink-只读客服）的 mode 是 **`advanced-chat`**（console API 确认）。

Dify 1.17.1 的两个 console 草稿运行入口都**不接受 advanced-chat**：

| 入口 | 对 advanced-chat 的响应 |
|---|---|
| `POST /console/api/apps/<id>/workflows/draft/run` | 404 `App mode is not in the supported list: {'workflow'}` |
| `POST /console/api/apps/<id>/chat-messages` | 404 `App mode is not in the supported list: {'chat','agent','agent-chat'}` |

即：**没有"就地预览草稿"的控制台接口**。这不是权限问题，是 Dify 的能力边界。

## 可用的两条路

**A. 临时应用预览**（仓库已有 `dify_workflow_trial.WorkflowTrial`）
- 在生产 workspace 导入一个临时应用 → 发布它 → 用它的 service key 跑真实模型 → `finally` 删除。
- 优点：跑的是**真实 Dify + 真实模型 + 真实知识插件**，且不动原应用的草稿与正式指针。
- 代价：会在生产 workspace **短暂创建并删除一个应用**（有审计痕迹）；导入期间占用该 workspace。

**B. 直接发布到正式应用**
- 跳过预览直接发布，再在正式入口验。
- 代价：没有"预览 B 而正式仍是 A"这一步，违背 acceptance.feature 的
  `Scenario: 预览用草稿，正式用发布版`，也会把未验证的提示词推给真实用户。

## 本轮判断

选 A 需要在**生产 workspace 创建临时应用**——这是写操作，超出"只读 + 改提示词"的授权，
必须由 owner 明确同意。选 B 不满足验收要求。

因此本轮停在这里，把问题交回，不自行选择。

## 另一个必须先解决的项

旧 SYSTEM 与新协议块有 3 处硬冲突（见 `2026-10-31-legacy-system-conflict.md`），
其中两条会**直接诱导**模型在事实片段外复述单号与状态——正是本次要拦的行为。
预览要测的正是"纯追加够不够"，所以冲突分析先于预览完成，顺序没有颠倒。
