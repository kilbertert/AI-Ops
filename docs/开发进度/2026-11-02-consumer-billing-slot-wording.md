# 消费者账单轮协议措辞修复

日期：2026-11-02　分支：`fix/consumer-billing-slot-wording`　PR：#1312
基线：`527e5a69`

## 里程碑目标

修掉消费者端「查订单账单」稳定失败的问题，并把 2026-11-02 三项补充验收
（知识问答回归、越权订单反例、账单链路）的结果固化下来。

## 已完成内容

1. **三项补充验收**（真机 + 服务端两半对齐，见
   [验证记录](../validation/2026-11-02-consumer-three-acceptance.md)）：
   - 知识问答回归：通过
   - 越权订单反例：通过
   - 账单链路：**失败**，根因定位后修复
2. **协议块第 81 行改措辞**：把「账单必须引用 billing_line，状态必须引用 status_line」
   改成按轮次区分的「账单查询只引 billing_line，状态查询只引 status_line；不要两种都写」。
3. **新增回归测试** `test_protocol_does_not_demand_both_segments_at_once`：
   钉住旧措辞不再出现、新措辞必须在。

## 为什么是第二次发作

2026-11-01 已为同一成因补过「只引用本轮实际给出的段」（已有测试
`test_protocol_says_to_use_only_the_segments_this_turn_actually_has`），
但**没有改第 81 行那句绝对措辞**。后面那句不足以抵消前面更早、更绝对的那条 ——
模型服从的是前者。这次把诱因本身改掉。

## 验证结果

```
tests/consumer/test_prompt_fact_segments.py    32 passed
tests/consumer（全量）                         558 passed
```

CI：spec-tree lint pass、Workflow policy pass（补 `No-ADR` 说明后）、verify 见 PR。

## 已知缺口

- **合并部署后必须重新发布**：`publication_settings` 会把草稿里的协议块与
  `_protocol_block()` **逐字比对**，措辞一改，旧草稿就不再相等。
  步骤：Dify 原生 `publish_workflow()` → 再 `POST /v1/dify/publish` 推进路由 revision。
  不做这一步，修复不生效。
- 账单轮的真机验证要在重新发布之后做，本里程碑提交时尚不能宣称已通过。

## 下一步

1. 合并 → 等 CD 部署 41
2. 重新发布 Dify app `394b5399`，确认 `chat_route_bindings.consumer_service` revision 推进
3. 真机复验账单提问：期望 `completed`，且正文只出现账单事实段（金额/支付/退款），
   不出现状态断言
