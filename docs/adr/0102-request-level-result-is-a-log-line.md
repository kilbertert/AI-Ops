# 102. 请求级结果用日志行观察，不再往指标表里加分母

日期：2026-10-23
状态：已采纳
影响版本：`src/aiops_diagnostics/request_observation.py`（新增）、
`src/aiops_diagnostics/gateway_api.py`、`README.md`、
`docs/agents/consumer-order-chat/{observability,qa-plan,README,single-tenant-enable-change-ticket}.md`

## 背景：一半的请求没有留下任何痕迹

个人订单状态查询（`POST /v1/assistant/questions` 的确定性分支）此前只有成功一侧可见：
一条 `agent_run_metrics` 的 `qa/completed` 行，**但没有 `duration_ms`、HTTP 状态或 `qa_id`**。
所有拒绝 —— 缺读权限 403、订单不可见 404、租户未登记 404、CPMS 502 ——
**一条记录都不写**。独立探测在 41 上逐条实测过这个形状。

后果不是"看板少一块"，而是变更单的**停止条件不可判定**：日志里一个 404，分不清它是
个人查询分支拒的、路由前那道订单授权闸拒的，还是别的入口拒的。"拒绝率超过阈值就停用"
这句话在没有分母、没有错误码的情况下无法执行。

## 决定

**每次候选请求写恰好一行结构化日志，不新增数据库表、不新增指标类型。**

```
INFO aiops.assistant_question assistant_question_result \
  request_id=cor_<12hex> route=consumer_order_chat stage=<stage> \
  outcome=<completed|refused|failed> http_status=<int> error_code=<CODE|-> \
  tenant_hash=<sha256[:16]|-> qa_id=<qa_…|-> duration_ms=<int>
```

三个实现要点，每个都有代价相当的反面：

1. **`request_id` 服务端生成**。取客户端头部更省事，但那样调用者可以自己决定关联、
   甚至故意让两个请求撞号。不读任何请求头是这里唯一安全的取值方式。
2. **分类谓词与路由谓词是同一个**，且在身份**已验证之后**才求值。因此 401 永远不会被记成
   "命中个人查询"，（更重要的）租户不可能来自客户端自报的头。代价是这段代码排在
   `caller, decision = identity` 之后，必须保证它不改变任何分支顺序 —— 它只读值、不返回。
3. **未预期异常先记录再原样抛出**。Starlette 的 `ServerErrorMiddleware` 在应用中间件之外，
   异常不会以 `send` 消息经过本模块；不显式捕获就会留下"失败了但没有任何痕迹"那一类空洞。

## 被否掉的路线

| 路线 | 为什么不行 |
|---|---|
| **在 `_consumer_order_chat` 里补 `_record_route_metric`**（上一版变更单的"约 3 行"建议） | **实测不成立**：`orders:read` 鉴权发生在该函数的 `try` **之前**，显式 `payload.order_no` 还可能在路由前就被拒。补在函数里覆盖不到这两类 |
| 新增一张 `request_outcome` 指标表 | `agent_run_metrics` 的保留期、租户隔离与形状都是承重的；再开一张同义表会出现两个分母，而"哪个才是真的"会成为下一个问题 |
| 用 uvicorn 的 access log | 它没有 `qa_id`、没有错误码，也不区分入口；打开它会把全站请求都打出来 |
| 复用 `order_auth` 子调用指标当请求结果 | 它是**子调用**：一次请求可能写多行，混进请求分母会把拒绝率算错 |

## 计时契约

起点是 ASGI scope 进入中间件时（身份解析**之前**），终点是响应交给服务器之后。
覆盖身份解析、路由前授权闸、SQL 选单、CPMS 读取与证据落盘；**不覆盖** TLS、
nginx/反代排队、连接建立与客户端下载。

这条契约的直接后果是：变更单里原来的两个数作废 —— `139/148 ms` 是
`assistant_questions.created_at → completed_at`（SQL 与 CPMS **之后**的一段），
"22 秒"只是依赖段之和、不含身份解析与连接建立，**不是整请求硬上限**。

## 后果

- 变更单的停止条件改成以结果行数为分母，并按 `outcome` 分类；样本 < 20 条时只报计数与极值；
- **仍判不了**的三项写进变更单：端到端时延、按 `request_id` 跨 nginx 串联、
  其他入口（`GET /v1/orders/{no}/charging-status`、M3 委派）的同类观察；
- 本片**不改**业务行为：鉴权、选单、状态回答、模型路由与开关语义一字未动；
  写日志抛错被吞掉，不影响响应。
