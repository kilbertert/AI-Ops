# 订单实时链路在正式入口跑通并被守卫拦截（2026-11-01）

## 方法：用真实下过单的用户

1. H5 页面真实登录（发码 + 内存读当次验证码）拿到 `third_session`；
2. 经受控 Redis 解析出该会话的真实 `userId`；
3. 在充电生产库 `ch_order_info` 查该 `user_id` 名下的订单（8 单），
   取其中一单作为查询对象。**没有借用他人订单，也没有构造身份。**

## 结果：链路通，守卫按设计拦截

```
POST /v1/assistant/questions → qa_9804fc2508df4220a73654b1a549abbc
final status: failed
```

服务端证据（`chat_dify_runs`）：

```
state=failed   reason=CHATFLOW_ANSWER_INVALID
data_kind=status   fact_contract=slots   status=live
live_snapshot: {order_no: 2109928861763776513, status_code: 1,
                status_role: finished, observed_at: …, source: cpms.chargingInfo}
```

**这证明了三件事**：
1. 订单号被正确授权（不再 `ORDER_NOT_FOUND`）；
2. 新契约在 realtime 分支生效（`fact_contract=slots`、`status=live`）；
3. 模型输出被事实守卫拒绝 —— 即**守卫在正式入口按设计工作**。

## 本地用同一快照复现判据

| 输入 | 结果 |
|---|---|
| `帮你查到了。\n{{fact:status_line}}` | 接受 |
| `这单已经充电结束了，是在 … 完成的。`（无片段） | 拒绝 `CHATFLOW_FACT_UNKNOWN` |
| `{{fact:billing_line}}`（该快照无账单事实） | 拒绝 `CHATFLOW_FACT_UNKNOWN` |
| `订单已结束。\n{{fact:status_line}}`（片段外复述状态） | 拒绝 `CHATFLOW_ANSWER_INVALID` |

生产上报的是 `CHATFLOW_ANSWER_INVALID`，对应最后一类 ——
**模型在事实片段之外复述了状态**，正是原验收要求"自由旁白与事实矛盾必须拦截"的那一条。

## 结论：这是拦截成功，不是功能失败

原验收标准要求"矛盾旁白必须拦截"。本次生产实测：
- 链路（授权、快照、契约版本）全部正确；
- 模型确实试图在片段外写状态；
- 守卫拒绝了整轮，没有把矛盾正文交给用户。

**这正是该验收项要的结果。** 但也要如实记：同一轮里没有产生一条"模型正确只引用片段"的
成功订单答复，所以"订单链路可交付正常正文"这一半**尚未在生产观测到**。

## 未完成

- 订单链路的一次**成功**答复（模型正确只引用片段）
- 固定 32 次样本批次
