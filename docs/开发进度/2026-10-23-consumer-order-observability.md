# 2026-10-23 · 个人订单查询的可归因结果与耗时观察

## 目标

让 `POST /v1/assistant/questions` 上的个人状态查询**成功、拒绝、异常都可归因**，并测到
**整请求**耗时，使 #1019 变更单的停止条件可以真正执行。生产开关保持关闭。

## 已完成

- 新增 `src/aiops_diagnostics/request_observation.py`：ASGI 中间件生成服务端
  `request_id`，每次**候选**请求写恰好一行 `assistant_question_result`；
- `gateway_api.py` 接线：身份已验证后分类，两个异常处理器记录稳定错误码，
  `_consumer_order_chat` 内记录 `stage` 与成功请求的 `qa_id`；
- 覆盖鉴权失败、路由前订单授权拒绝、分支内拒绝、CPMS 失败、未预期异常；
- 未预期异常记录后**原样抛出**，响应仍由 Starlette 生成，业务行为不变；
- 观察写入抛错不影响响应；
- 文档：新增[观察契约](../agents/consumer-order-chat/observability.md)，扩展
  [QA 计划](../agents/consumer-order-chat/qa-plan.md)（COC-09～12），更新
  [手册](../agents/consumer-order-chat/README.md)，**更正变更单**里作废的耗时口径与停止条件。

## 当前分支 / 提交

`feat/consumer-order-observability`，基线 `3e212f5`（main）。

## 验证结果

| 项 | 结果 |
|---|---|
| 既有回归 | **56 passed**（未改断言、未加 skip、未降标准） |
| 新增组件测试 | **18 passed**（`tests/test_request_observation.py`） |
| 合计 | **74 passed** |
| 真实 HTTP | `observe_consumer_order_result.py`：**20/20 检查通过**，10 条结果行（成功 5 / 拒绝 3 / 失败 2） |
| 反向验证 1：不记失败 | **7 条转红**（5 条拒绝：403 / 订单不可见 404 / 租户未登记 404 / CPMS 502 / 路由前 404，+ 未预期异常 + 真实 HTTP 用例） |
| 反向验证 2：计时移到 QA 落盘后 | **1 条转红**（`test_duration_covers_work_that_happens_before_the_qa_row_exists`） |
| 还原后 | 74 passed |
| `ruff check` / `ruff format --check` | 干净 |

## 已知缺口

- 只覆盖 `POST /v1/assistant/questions`；`GET /v1/orders/{no}/charging-status`、
  M3 委派入口没有等价行（任务书划定的范围）；
- `agent_run_metrics` 的 `qa` 行仍无 `duration_ms`（未改，避免动指标写入与保留期语义）；
- `duration_ms` **不含** nginx 与网络，`request_id` 也只在网关这一层，跨层串联仍按秒对齐；
- 限流、账号灰度仍未做（明确不做）。

## 下一步

由 Codex 独立复验后，再决定是否执行单租户开放。**本片不开窗、不提交业务问题、
不调用模型，生产开关保持关闭。**
