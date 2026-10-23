# 个人状态查询的结果观察契约

日期：2026-10-23
状态：**已实现并验证**。生产开关仍然关闭；本片只补观察能力。

## 一、为什么要这一行日志

Codex 在 41 上的独立探测（`RECEIPT-2026-10-23-codex-probe.md`）量出两个洞：

1. 成功写了一条 `qa/completed` 指标，但**没有 `duration_ms`**，也没有 HTTP 状态和 `qa_id`；
2. 403 `INSUFFICIENT_SCOPE`、404 `ORDER_NOT_FOUND`、404 `TENANT_ENDPOINT_UNRESOLVED`、
   502 `CHARGING_STATUS_UNAVAILABLE` —— **一条都没写**。

后果不是"数字不好看"，而是变更单的停止条件**不可判定**：日志里一个 404，
分不清是个人查询分支拒的、路由前那道闸拒的，还是别的入口拒的。

**没有另建指标表**：`agent_run_metrics` 的保留期、租户隔离和形状都是承重的，
再开一张同义表会出现两个分母。本片复用既有日志链路（`gateway_logging` 已经给
`aiops.` 命名空间装了 root handler），每次候选请求**只写一行 INFO**，不写库。

## 二、一行长什么样（固定字段顺序）

```
INFO aiops.assistant_question assistant_question_result \
  request_id=cor_<12hex> route=consumer_order_chat stage=<stage> \
  outcome=<completed|refused|failed> http_status=<int> error_code=<CODE|-> \
  tenant_hash=<sha256[:16]|-> qa_id=<qa_…|-> duration_ms=<int>
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `request_id` | **服务端生成**的关联标识 | 取客户端头的做法会让调用者自己决定关联、故意撞号；这里不读任何请求头 |
| `stage` | 请求走到哪一步才结束 | 见下表。用来把同一状态码的两条路径分开 |
| `outcome` | `completed` / `refused`（4xx 表示"我们说不"）/ `failed`（5xx 或逃逸异常） | 4xx 与 5xx 分开，不合并成"失败" |
| `http_status` | 该请求最终的 HTTP 状态 | 异常逃逸时为 `500`（见第四节） |
| `error_code` | 稳定错误码 | 只接受 `^[A-Z][A-Z0-9_]{0,63}$`，其余一律写 `UNKNOWN`，防止异常文本或 URL 混入 |
| `tenant_hash` | `sha256(租户 id)[:16]` | **只来自已验证身份**；身份没建立就没有这一行，不存在"客户端自报租户" |
| `qa_id` | 成功时那条 QA 的 id | 拒绝路径写 `-`，**不伪造 QA** |
| `duration_ms` | 整请求耗时 | 起点与终点见第三节 |

### stage 取值

| stage | 说明 |
|---|---|
| `classified` | 已判定为候选，随即被拒（例如缺读权限） |
| `pre_route_order_authorization` | 在进入分支**之前**被显式 `order_no` 的订单授权闸拒掉 |
| `order_chat_authorization` | 分支内身份/入口判据拒绝 |
| `order_chat_lookup` | 分支内选单失败（含"没有本人订单"） |
| `order_chat_cpms` | 走到 CPMS 解析/读取（含租户未登记、CPMS 坏结果） |
| `order_chat_evidence` | 已写 QA/证据，成功结束 |
| `unclassified` | 不适用（非候选）——不会出现在输出里 |

## 三、计时契约（这一节是本片最重要的更正）

**起点**：ASGI scope 进入中间件时（`time.monotonic()`），也就是在身份解析之前。
**终点**：响应已交给服务器之后。

覆盖范围：

| 覆盖 | 不覆盖 |
|---|---|
| 身份解析（含 introspection / 会话解析） | TLS 握手、证书校验 |
| 路由前订单授权闸 | nginx / 反代排队、连接建立 |
| 分支内 SQL 选单 | 服务器自身读写缓冲里、scope 创建之前的时间 |
| CPMS HTTP 读取（`timeout=10`） | 客户端下载响应体的时间 |
| 证据落盘与 QA 写库 | — |

**为什么必须写清"不覆盖"**：#1019 变更单里的 `139/148 ms` 是
`assistant_questions.created_at → completed_at`，也就是 **QA 落库之后**的一段 ——
SQL 选单和 CPMS 读取都在它之前完成，所以那两个数**不是整请求耗时**。
同理，变更单里"最坏 22 秒"只把 SQL（每条 ≤3 s × 4）与 CPMS（10 s）相加，
**不含**身份解析、连接建立等阶段，因此**不能**称为整请求硬上限。

本片新增的 `duration_ms` 是本节声明的范围；它仍不是端到端（不含 nginx 与网络）。

## 四、未预期异常怎么办

Starlette 的 `ServerErrorMiddleware` 在应用中间件**之外**，所以逃逸异常不会以
`send` 消息经过本模块。中间件因此显式 `except Exception`：记一行
`outcome=failed / error_code=UNHANDLED_EXCEPTION / http_status=500`，然后**原样抛出**，
响应仍由 Starlette 生成 —— 业务行为一字未改。

其他拒绝路径在 `StandardAPIError` 处理器里补记稳定错误码；422 在
`RequestValidationError` 处理器里记 `INVALID_REQUEST`。

## 五、什么不会被记录（故意的）

- **身份未建立**的请求（401/403 出自解析器）：handler 里的分类代码根本不会执行，
  所以既不会出现"命中个人查询"的字样，也不会有租户；
- **普通问答**：分类谓词与路由谓词**是同一个**（启用 + consumer + 无快捷动作 +
  zh/zh-Hant/en + `is_order_status_question`），不满足就不记；
- **功能关闭**时：`consumer_order_chat_enabled=false` ⇒ 不是候选 ⇒ 不记。

## 六、怎么观察（可执行命令）

以下都在 41 上、只读：

```bash
# 1) 本次窗口内所有候选请求的原始行（按 journal 时间）
journalctl -u aiops-gateway-41 --since "2026-10-23 15:00:00" --no-pager -o cat \
  | grep 'assistant_question_result'

# 2) 按 outcome 分组计数（分母是"候选请求"，不是全站问答）
journalctl -u aiops-gateway-41 --since "-1 day" --no-pager -o cat \
  | grep -o 'outcome=[a-z]*' | sort | uniq -c

# 3) 拒绝都拒在哪一步
journalctl -u aiops-gateway-41 --since "-1 day" --no-pager -o cat \
  | grep 'assistant_question_result' \
  | grep -o 'stage=[a-z_]* .*error_code=[A-Z_-]*' | sed -E 's/ .* / /' | sort | uniq -c

# 4) 耗时分布（样本不足时不要算 p95，见第七节）
journalctl -u aiops-gateway-41 --since "-1 day" --no-pager -o cat \
  | grep -o 'duration_ms=[0-9]*' | cut -d= -f2 | sort -n

# 5) 成功行与 QA 行的关联（请求侧）
#    行里的 qa_id 直接对上数据库：
#    SELECT qa_id, status, created_at FROM assistant_questions WHERE qa_id = '<上面那行的 qa_id>';
```

**分母口径**：`outcome` 的四个分类都以 `assistant_question_result` 行数为分母。
`agent_run_metrics` 里的 `order_auth` 是**子调用**指标（一次请求可能写多行），
**不能**与这里的请求结果相加或相除。

`qa_id` 只在 `outcome=completed` 时有值；`-` 表示该请求没有、也不应该有 QA。

## 七、样本不足时怎么办

- 少于 **20** 条：只报计数与最大/最小值，**不报 p95、不报百分比**；
- 一天内 `outcome=completed` 为 0：写"未观测到成功"，不推断成"功能坏了"；
- 没有任何一行（例如开关未开）：写"未观测"，**不能**写成"没有拒绝发生"。

## 八、已知缺口（本片没做的部分）

| 缺口 | 现状 | 为什么不在这片 |
|---|---|---|
| 其他入口的同类观察 | `GET /v1/orders/{no}/charging-status`、M3 委派入口没有等价行 | 用户划定的范围只有 `POST /v1/assistant/questions` |
| `agent_run_metrics` 的 `qa` 行仍没有 `duration_ms` | 未改 | 改它要动指标写入路径与保留期语义，超出"观察请求结果"的范围 |
| End-to-end 耗时 | 不含 nginx 与网络 | 要把它算进来需要在 nginx 侧加字段，属于另一处的改动 |
| 按请求 id 跨层串联 | 只有本行的 `request_id`，nginx 日志没有对应字段 | 同上 |
| 限流 / 账号灰度 | **仍未做** | 任务书明确不做 |

以上缺口意味着：**停止条件里凡是依赖"逐请求归属"的都能判了**，
凡是依赖"端到端时延"或"按请求跨层串联"的**仍然判不了**，变更单里已按此改写。
