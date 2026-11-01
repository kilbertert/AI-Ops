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

请求归属（按 #1285 的要求先证明，再判成因）：

```
tenant_id  = 1942105476598861824
binding    = consumer_service:production-41:1942105476598861824   ← 入口 consumer、场景 consumer_service
caller     = service:java-bff      ← 不是 H5 会话身份
```

注意 `caller` 是**服务身份**（公司 BFF 转发），不是我的验收账号本身；
这与 `chat_dify_runs` 历史记录一致（该链路一向由 `service:java-bff` 代表用户调用）。
它**不代表**这是"别人的请求"——`live_snapshot.order_no` 确是我用验收账号解析出的本人订单。

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

## 关于拒绝原因：只能确定"被守卫拒绝"，不能确定是哪一条

生产记录里只有 `CHATFLOW_ANSWER_INVALID`。该码**同时覆盖**多种情形：
片段外复述状态、回答格式不符、字段缺失等（见 `consumer_facts` 与 `consumer_documents`
的多个 raise 点）。表格里最后一行只是**本地复现**能产生同一错误码的输入之一，
**不能据此断定生产那一轮就是"片段外复述"**。

要判定具体原因，需要该轮的原始模型输出，而它没有被落库（`reply_source`、`snapshot` 均为 null）。
所以准确表述是：**模型输出被事实守卫拒绝，具体触发条件未取证。**

## 结论

已验证的部分：
- 订单号被正确授权（不再 `ORDER_NOT_FOUND`）；
- 新契约在 realtime 分支生效（`fact_contract=slots`、`status=live`）；
- 守卫拒绝了这一轮输出，没有把不合格正文交给用户。

**未验证**：拒绝的具体原因；以及"订单链路可交付正常正文"——本轮没有产生任何成功的
订单答复，这一半尚未在生产观测到。

## 未完成

- 订单链路的一次**成功**答复（模型正确只引用片段）
- 固定 32 次样本批次
