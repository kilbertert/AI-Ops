# 2026-11-04：度量维度生产点引用命名常量（#799 收口）

## 目标

把 #799 验收第 3 条剩下的「其余生产点仍是裸字面量」收掉，让「新增一个路由类型要改
哪几行」有答案：改 `metrics_store.py` 一处。

## 已完成内容

- `metrics_store.py` 新增 `ROUTE_CLARIFICATION`、`ROUTE_OPERATIONS_ANALYSIS`。
- `gateway_runtime.py` 三处、`gateway_api.py` 一处的字面量改为引用它们。
- `tests/gateway/test_metrics_dimensions.py` 新增两条：不许新增裸字面量；白名单只许减少。

## 当前分支或提交

`fix/metrics-producer-constants`，基线 `9b24aad7`。

## 验证结果

- `uv run pytest tests/gateway/`：204 passed
- 两条新守卫各做一次反向验证（改回裸字面量 / 改成词表外值 → 红 → 恢复）
- `ruff check` / `ruff format --check`：通过

## 已知缺口

- **未部署**。与 ADR-0206 同一边界：部署后复测 `route_type=trial` 的指标行。
- operator 预览路径仍不写指标（#799 评论里的欠账，另需决策）。
- `retrieval_status` 的托管值仍未显式化（#799 验收第 5 条）。
