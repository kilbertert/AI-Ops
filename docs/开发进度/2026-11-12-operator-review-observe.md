# 管家答复复核降为观察（ADR-0252）

## 目标

- 用户要求本阶段先让诊断成功且顺利完成：语义复核（unsupported_claim /
  unsupported_station / review_uncertain / review_unavailable 等）不再一票否决
  交付，只保留观察记录；授权与数据完整性闸不变。

## 本轮动作

- `_complete`：交付闸由 `verdict != allow` 改为 `model_failure is not None`；
  `delivery_mode` 按有无答复给出 `diagnosis`/`evidence_only`；复核 trace 的 status
  只在复核自身未评估（unavailable）时记 refused，verdict 原因照常落 detail。
- `_worker`：移除两处「`task.answer_review` 非空 → `OPERATOR_ANSWER_NOT_GROUNDED`」
  翻译；派发成功但 run 未推进先查 `task.completion_refusal`（终局闸拒直接归原码
  不重派），其余才按 `no_answer_accepted` 重采样，上限后 `UNSTABLE`。
- 独立 Hack 回执修订：复核后授权/范围闸拒并入既有 stamp 约定（原码落 run.reason、
  答复吸收不投递），新增 `completion_refusal` 字段防误归因重采样；`review()` 收敛
  AttributeError 为 unavailable；日志页在答复已交付时把复核原因标为「复核观察」；
  verify_operator_cpms.py 断言同步新语义。
- 测试：`test_operator_answer_review` 两个闸语义测试翻转为「交付+verdict 记录」，
  新增终局闸拒不重派回归与复核异常收敛用例。
- 文档：ADR-0252、`evidence-delivery.md`、`operator-cpms/README.md` 同步。

## 分支与验证结果

- 分支：`fix/operator-review-observe`（worktree `AI-Ops-operator-review-observe`），
  主提交 `54dfb38e`（rebase 于 origin/main `25885d34`），Devin 复审补齐跟进于后续提交。
- 验证结果（主补丁口径）：定向 pytest（operations/execution/dify）
  1743 通过/1 跳过/0 失败；全套件 5033 通过/9 跳过/0 失败；
  verify-aiops-gateway 通过；ruff check/format 干净。
  PR #1494 CI（verify/policy/spec-tree/mirror）通过。
- 独立 Hack（deepseek-v4.1-flash，148 轮）发现 4 项已全部修订并回归测试；
  Devin 复审 2 项（派发异常分支闸拒兜底、里程碑记录字段）的提交在合并时
  遗漏，由跟进补丁 `fix/operator-review-observe-followup`（PR #1495）补齐，
  其进度与验收记录见 `2026-11-12-operator-review-observe-followup.md`。
- 已知缺口：ED4 实机容器验证与 llmdoc 同步待合并后完成；生产复核后闸拒频率留观察。

## 交付与边界

- 保留：复核调用本身（verdict 照常计算入快照/轨迹/行级日志）、`contains_secret`、
  订单/站点再授权、scope/grant、EVIDENCE_*、工具契约、model_failure 证据兜底。
- 已知观察噪音：gun 编号被摘录成站名会产生 `unsupported_station` 原因码（不拦截，
  恢复拦截前再评估修正）。
- 验收明细：见 `docs/validation/2026-11-12-operator-review-observe.md`。
