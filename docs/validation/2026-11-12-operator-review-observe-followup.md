# 派发异常分支闸拒兜底：实现验收

2026-11-12 UTC。ADR-0252（PR #1494，`e9827d35`）合并后的 Devin 复审跟进补丁，
候选 `fix/operator-review-observe-followup` @ `199e9a57`，PR #1495。
主补丁验收见 `2026-11-12-operator-review-observe.md`，本文只记本补丁。

## 变更面

- `operator_cpms.py::_worker`：`except AgentRunDispatchError` 分支新增
  `task.completion_refusal` 检查（`raise OperatorRefused(...) from None`），
  终局闸拒不再因运输层错误被误当「没交出答复」重派。
- `operator_cpms.py::_failure_code`：`OPERATOR_SCOPE_CHANGED` 归入
  `DEPENDENCY_UNAVAILABLE` 桶，stamp 兜底路径与直接闸拒归因一致。
- `test_operator_answer_review.py`：`test_post_review_refusal_survives_stamp_failure`
  参数化 `dispatch_raises`（False/True），同测两条 worker 路径。
- 两文档：本进度篇 + 本验收篇；主补丁进度文档标注跟进独立成篇。

## 自动化

- `uv run pytest tests/operations/test_operator_answer_review.py -k stamp_failure`：
  2 变体全过（`..`）。
- `uv run pytest tests/operations/test_operator_answer_review.py`：31 项全过。
- `uv run ruff check` / `format --check`（两改动文件）：干净。
- PR #1495 CI：Workflow policy（含 ADR/No-ADR 判定）、verify、
  spec-tree lint、mirror 全绿；Devin Review 额度耗尽跳过全量。

## 未验范围

- 未重跑全套件（主补丁已 5033 绿，本补丁为同语义兜底 + 一个映射项 +
  测试参数化，影响面限于 `_worker` 拒径）。
- 未跑 verify-aiops-gateway（无对外行为面变化；交付闸语义同主补丁）。
- 生产端闸拒频率观察、ED4 实机、llmdoc 同步沿用主补丁清单，合并后一并处理。
