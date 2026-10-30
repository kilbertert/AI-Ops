# C 端答复改版的 41 真机核对（通过）

日期：2026-10-30（UTC）。环境：41（生产）。方式：**只读**——读 Redis 会话、读业务库、
向公网入口发查询。未改任何配置、未启用任何开关、未写业务数据。

被验对象：#1175（`5de086c3`，排版）+ #1202（`0b58d502`，结论与白话），
决策见 [ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)。

> 本记录是同日早先那版
> [2026-10-30-consumer-plain-answers-live.md](2026-10-30-consumer-plain-answers-live.md)
> 的**更正与续记**。那一版把结论写成「该面在 41 上未启用」，是错的；按「一条记录一个文件」
> 的约定它保留原样（带更正横幅），本文件给出通过的证据与错因，验收时间线因此完整。
> 开发进度见[同轮里程碑记录](../开发进度/2026-10-30-consumer-plain-answers-live.md)。

## 部署事实

| 项 | 证据 |
| --- | --- |
| 线上修订 | `curl http://172.18.0.1:8788/health` → `version=1.0.0+0b58d502adc5`，`business_mutations=disabled` |
| 新代码在位 | `/opt/aiops-41/src/aiops_diagnostics/consumer/consumer_plain.py`，mtime `2026-10-30 13:48:12 +0800` |
| CD | run `41010446836` success |

## 先确认「用户看到的到底是哪条面」

反馈截图的文件时刻是 `2026-10-30 03:41`。41 的 `assistant_questions` 在该分钟内**只有一条**：

```
qa_id     qa_5c85458e34f94b9181ba0a82515575c9
created   2026-10-30T03:41:13.354675+00:00
question  帮我检测（REDACTED）这个订单的充电异常
status    completed
result    订单 2115290621004312577 本次查询状态：充电结束（status_code=1，status_role=finished）。
          来源：cpms.chargingInfo；观察时间（UTC）：2026-10-30T03:41:14.504254+00:00。
          这是本次业务状态观察，不是故障原因诊断。
run       chat_dify_runs: binding_key=consumer_service:production-41:1942105476598861824
          app_id=394b5399-7ad9-428e-b2b0-cc7f05720a81
```

这就是反馈里那段"干巴巴"的原文：`status_code=1`、`status_role=finished`、`cpms.chargingInfo`、
带微秒的 ISO 时间。**截图就是这条面**，而它正是 #1175／#1202 改的那条渲染路
（`chat_execution.answer_from_run` 对快照复用 `status_answer`，不调模型）。

## 真机核对结果（新代码）

真实会话（租户 `…861824`，该租户是唯一登记了充电端点的租户）+ 本人真实订单
`2107546575906942977`，公网入口 `POST /v1/assistant/questions`：

状态问法（`status=completed`）：

```
这单当前的状态是「充电结束」。

订单号：2107546575906942977
查询于：2026-10-30 06:24:55 UTC

这只是我查到的业务状态，不代表故障原因。
来源：充电状态记录
```

账单问法（`status=completed`）：

```
这单的账单合计 2.4，系统记录的支付金额是 2.4，没有退款记录。

· 订单金额 2.4 —— 这单的总计费；币种接口未提供，金额单位请与原订单页面核对
· 支付金额 2.4 —— 账务系统里记的数，不等于银行已经扣款
· 退款：没有退款记录 —— 有这条记录也不代表银行已经到账，已退金额 0.0

我没有发起任何付款或退款，只能读取记录。

订单号：2107546575906942977
来源：订单账务记录 · 2026-10-30 06:25:43 UTC
```

两种答复的 `cpms.*`／`status_code=`／微秒 ISO 都不再出现，第一段是结论。

## 探测方法（可复跑）

```bash
# ① 会话：从 41 的 Redis 取（前缀 AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX=app:3rd_session:）
#    令牌只在内存里传，不落盘不打印
# ② 冒烟：GET /v1/faq/recommendations 返回 200 才算会话有效
# ③ 该会话可归属的真实订单：ch_order_info 按 tenant_id + user_id 取（只读）
# ④ 提问：POST /v1/assistant/questions，按 retry_after_ms 轮询
```

**两条头名事实**（这次踩到的）：

- 走**公网入口**（`api.mall.qushiyun.com`）用 `third-session`（小写连字符）；BFF 注入
  `Authorization: Bearer <服务令牌>` 并映射成网关的 `X-Third-Session`。
- **直达 `172.18.0.1:8788` 不行**：网关依赖要求 `Authorization: Bearer`，只给会话一律
  `401 ACCESS_TOKEN_REQUIRED`。本机调试要走公网入口，或自己补服务令牌。

## 一次走错的路（保留，供下一个人省时间）

早先那一版记录写成了「41 上摸不到这条面、`consumer_order_chat_enabled=False`」。三处错：

1. **读错了文件**：开关在 `/etc/aiops-41/gateway.env`（systemd 的 `EnvironmentFile`），
   我去 `production.env` 里找。41 手册 §1.0／§1.2 本来就写明了这两个文件的分工，
   **知识在仓库里，是我没照着手册查**。实际值：`AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=1`。
2. **把端点登记读成开关**：`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 只登记了租户
   `1942105476598861824`；我随手挑的会话属于另一个租户，于是拿到
   `404 TENANT_ENDPOINT_UNRESOLVED`。那是"这个租户没登记端点"，不是"功能没开"。
3. **问法没进那条路由**：`is_order_status_question` 只认「订单＋状态／最新＋查」，
   「查订单的账单」不含状态词，因此落到 `409 CHAT_ROUTE_UNAVAILABLE`。
   账单答复在生产上是由 chatflow 取到 `cpms.orderDetail` 快照后走 `billing_answer` 的。

教训：**先按手册确认配置所在文件与开关值，再解释一次请求的返回码**；
一个 `404` 说的是"这一条没登记"，不是"这个功能没开"。

## 仍然未验

- 真机／原 H5 上的**观感**（结论句是否在第一屏、`·` 与编号在真机的对齐）——我看不到屏幕，
  只能验到文本。这一条仍需要人看一眼。
- 九种非中文译文**未经母语者复核**。
- 账单问法在生产上依赖 chatflow 取到 `cpms.orderDetail` 快照；本次只验了这一个租户的一单。

操作知识自检：本轮实际用到的步骤（会话取用、公网头名、开关所在文件、端点登记）
都已在 [41 手册](../agents/env-41-runbook.md) §1.0／§1.2／§4／§5 里；本记录只补了
"读错文件会把 404 读成功能未开"这一条判断经验。未把任何会话令牌、口令写进仓库。

## 附：CD 推进后的复跑（同日）

上面那轮核对时线上是 `1.0.0+0b58d502adc5`。此后 CD 又部署了 #1205，
`/health` 变为 `1.0.0+be78e7c21e10`。在同一租户同一订单上复跑一次状态问法：

```
这单当前的状态是「充电结束」。

订单号：2107546575906942977
查询于：2026-10-30 07:12:34 UTC

这只是我查到的业务状态，不代表故障原因。
来源：充电状态记录
```

结论不变：新格式在**当前**线上修订上仍然成立；#1205 只动管家的工具声明，
不影响这条渲染路。
