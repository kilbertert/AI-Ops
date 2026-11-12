# 派发异常分支闸拒兜底与归因补齐（ADR-0252 跟进）

## 背景

- PR #1494（ADR-0252，`e9827d35`）合并时，Devin 复审的两处修订在 force-push
  中留在 worktree 未提交。本补丁把这部分遗漏按正常 PR 流程交付。
- 分支：`fix/operator-review-observe-followup`（基线 `e9827d35`），候选提交
  `199e9a57`，PR #1495。

## 本轮动作

- `_worker` 的 `except AgentRunDispatchError` 分支补 `task.completion_refusal`
  检查：`_complete` 终局闸拒（码未落账、run 仍活动）撞上运输层错误时同样归
  原码、不重派——与正常返回未推进分支对称。
- `_failure_code` 补 `OPERATOR_SCOPE_CHANGED → OPERATOR_DEPENDENCY_UNAVAILABLE`
  映射：stamp 兜底路径与直接闸拒路径归因一致（此前落 DIAGNOSIS_FAILED）。
- `test_post_review_refusal_survives_stamp_failure` 参数化
  `dispatch_raises`，同一条用例覆盖 worker 两条路径的兜底。
- 进度文档 `2026-11-12-operator-review-observe.md` 标注本补丁独立成篇。

## 验证结果

- `test_operator_answer_review.py` 31 项全过（新增参数化用例两变体）。
- ruff check / format 干净（`B904` raise 链与 `E501` 换行已修）。
- PR #1495 CI：Workflow policy / verify / spec-tree lint / mirror 全绿；
  Devin Review 因额度跳过全量复审，但其在本 PR 的三条线程按发现修订。

## 边界

- 纯兜底与归因修正，不改交付语义；ADR-0252 记录已覆盖，无新决策（No-ADR 提交）。
- 未验范围与主补丁相同：ED4 实机容器验证、llmdoc 同步、生产闸拒频率观察。
