# 2026-10-23 · 个人订单查询可归因观察：验收记录

范围声明：本片只声明**新增观察能力**的本地验证范围。业务验收沿用
[#1015 的结论](../agents/consumer-order-chat/real-h5-status-acceptance.md)，
本片**没有**新增真实业务验收，也不改变任何业务行为。

## 新增的自动化检查

`tests/test_request_observation.py`（18 条），全部驱动**真实 API 路径**：

| 判据 | 用例 |
|---|---|
| 成功一行带真实 `qa_id`、202、`completed`、`order_chat_evidence` | `test_success_line_carries_qa_id_status_and_duration` |
| 耗时覆盖 SQL/CPMS 之前的阶段（两段各 +120 ms ⇒ ≥240 ms） | `test_duration_covers_work_that_happens_before_the_qa_row_exists` |
| 缺读权限 403 有行、`INSUFFICIENT_SCOPE`、无 `qa_id` | `test_missing_order_read_scope_is_refused_and_recorded` |
| 订单不可见 404 有行、**不产生 QA 与证据文件** | `test_unowned_order_records_the_refusal_and_creates_no_qa` |
| 租户未登记 404 可与"订单不可见"区分 | `test_unregistered_tenant_is_distinguishable_from_an_unowned_order` |
| CPMS 失败记 `failed` 而不是 `refused` | `test_cpms_failure_is_a_failure_not_a_refusal` |
| 显式 `order_no` 在路由前被拒也有行，`stage` 指得出来 | `test_explicit_order_refused_before_the_branch_is_still_attributed` |
| 未认证请求**一行都不写** | `test_unauthenticated_request_is_never_counted` |
| 普通问答不误标 | `test_ordinary_question_is_not_mislabelled` |
| 功能关闭不声称命中 | `test_feature_disabled_never_claims_the_branch` |
| 行里没有问题/回答/订单号/凭据 | `test_line_carries_no_question_answer_order_or_credential` |
| 未预期异常记 `UNHANDLED_EXCEPTION`，响应不变，异常文本不入日志 | `test_unexpected_exception_is_recorded_and_the_response_is_unchanged` |
| 日志 sink 抛错不改变响应 | `test_a_broken_log_handler_does_not_change_the_response` |
| 同秒并发 4 条：一请求一行、id 不重复 | `test_same_second_concurrency_keeps_one_id_and_one_line_per_request` |
| 真实 uvicorn + TCP 下同样成立 | `test_real_uvicorn_http_writes_one_line_per_request` |
| 非 http scope 直接透传 | `test_non_http_scopes_pass_straight_through` |
| 自由文本塞进 `error_code` 会被拒 | `test_format_line_rejects_free_text_in_the_error_code` |
| 未安装观察时返回 `None` | `test_observation_of_returns_none_when_not_installed` |

## 真实环境验证

```bash
uv run python .claude/skills/verify-aiops-gateway/scripts/observe_consumer_order_result.py
```

一次性数据根 + 真实 `aiops-gateway serve` + 真实 TCP。**身份解析是真的**
（真 `IntrospectionCallerResolver` 打回环 introspection 桩），**被测的观察实现是真的**；
只有订单行、UPMS 平台目录、订单授权源与 CPMS 是回环合成（它们在真环境是 MySQL
与外部服务，本机不该连）。证据文件里原样写清这一点。

本次运行 **10/10 检查通过**，共 10 条结果行（成功 5 条含 `qa_id`；拒绝 4 条；失败 1 条），
`duration_ms` 3～79 ms，`request_id` 10 个互不相同，行内不含问题、订单号、令牌或原始租户。

证据：`var/verify-evidence/consumer-order-observation.json` 与同目录
`consumer-order-observation.gateway.log`（两者都是本地运行产物，未入仓）。

## 反向验证（先红后绿）

| 变异 | 结果 |
|---|---|
| `emit()` 只记成功、不记失败 | **7 条转红**：5 个拒绝场景 + 异常场景 + 真实 HTTP 用例 |
| 把计时起点移到 QA 落盘之后（`attach_qa` 里重置 `started`） | **1 条转红**：`test_duration_covers_work_that_happens_before_the_qa_row_exists` |
| 还原 | 74 passed |

第二个变异特意写成"在 `attach_qa` 里重置起点"，因为那正是变更单旧口径
（`created_at → completed_at`）的形状 —— 只有这条判据能抓住它。

## 失败与修复（过程中的真实错误）

1. **`error_code` 空值渲染成 `UNKNOWN`**：成功行本该是 `-`。测试的分组正则先把这件事暴露出来；
2. **测试正则写错**：`qa_id` 组用了 `[\-0-9a-f]+`，漏掉 `q`/`a`，导致所有成功行解析失败；
3. **真实 HTTP 首跑全部 401**：用错了 audience 变量名 —— 正确的是
   `AIOPS_GATEWAY_STANDARD_API_AUDIENCE`，不是 `..._INTROSPECTION_AUDIENCE`。
   这一条只有真起进程才会暴露，`TestClient` 注入替身时看不见；
4. **夹具里把模型路由写成 assert**：普通问答与"功能关闭"用例要经过模型路由，
   该替身必须惰性返回而不是断言 —— 否则测的是夹具而不是被测代码。

## 未完成的部分（不得写成已验证）

- 真实登录用户、真实 MySQL 订单、真实 CPMS 的业务验收：**沿用 #1015，本片未新增**；
- 端到端时延与按请求跨层串联：**无法判定**，见[观察契约](../agents/consumer-order-chat/observability.md)第八节；
- 其他入口（`GET /v1/orders/{no}/charging-status`、M3 委派）没有等价观察。

## 生产影响

零：未开生产窗口、未提交业务问题、未调用模型、未改服务器配置。开关仍关闭。
