# PROGRESS — 个人订单查询可归因观察

## 开工记录（≤10 行）

1. 目标：`POST /v1/assistant/questions` 上，个人状态查询失败也能独立归因，并测到**整请求**耗时。
2. 顺序：契约/QA → 观察实现 → 组件测试 → 真实 HTTP → 两处反向验证 → 变更单口径更正。
3. 复用：`gateway_logging` 的既有权重与格式、`_record_route_metric` 的脱敏约定；**不**新增表/指标类型。
4. 关联标识：ASGI 中间件按请求生成 `cor_<12hex>`，存 `scope["state"]`；并发天然隔离。
5. 分类：身份建立后，满足 `consumer + 无快捷动作 + 中/繁/英 + is_order_status_question` 才记为候选；
   **身份未建立（401）一律不记**，避免把未知读成"命中个人查询"。
6. 计时：ASGI 进入 → 响应发送完成。覆盖路由前拒绝、函数内失败、未预期异常。
7. 最大风险：在 `assistant_questions` 里**提前计算候选谓词**可能被视为改变路由；实现上只用已就绪的
   `caller/decision/payload/language` 求值，不改变任何分支与顺序。
8. 反向验证：删掉失败记录 ⇒ 失败场景测试转红；把计时移到 SQL/CPMS 之后 ⇒ 耗时测试转红。
9. 不做：限流、账号灰度、生产窗口、模型调用、改公司仓。
10. 提醒：`docs/validation.md` / `docs/开发进度.md` 是只读归档，本片记录必须新增独立文件。

## 状态

| 阶段 | 状态 |
|---|---|
| 0. 基线 | ✅ `56 passed`（`uv run pytest -n 0 tests/test_consumer_order_chat.py tests/test_agent_metrics.py`） |
| 1. 契约 + QA | ✅ |
| 2. 观察实现 | ✅ `request_observation.py` + `gateway_api` 接线 |
| 3. 组件测试 | ✅ 18 条（`tests/test_request_observation.py`） |
| 4. 真实 HTTP | ✅ `observe_consumer_order_result.py`，**20/20** 检查通过（10 条结果行：5 成功 / 3 拒绝 / 2 失败） |
| 5. 反向验证 | ✅ 删失败记录 → 7 红；计时移到 QA 之后 → 1 红；还原全绿 |
| 6. 变更单口径 | ✅ |
