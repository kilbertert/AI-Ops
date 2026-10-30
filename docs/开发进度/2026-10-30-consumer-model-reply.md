# 客户端编排页的提示词可以写正文

分支`feat/consumer-model-reply`，基线`425b5f79`。
设计取舍见 [ADR-0187](../adr/0187-consumer-chatflow-prompt-writes-the-reply.md)，
契约见 [acceptance.feature](../acceptance/consumer-model-reply/acceptance.feature)。

## 目标

产品从第一天起要的就是：**在 Dify 编排页改提示词就能调整客户端智能体的说话方式**。
此前做不到，因为 `answer` 节点的正文被丢弃（网关只取 `kind`/`faq_id`），用户看到的正文
由代码按快照渲染。本片给提示词开一个写正文的口子，同时把"不许编数字"做成可执行校验。

## 已完成内容

- 新增 `consumer/consumer_reply.py`：`reply` 的校验与包装。
  三条判据 —— 快照里每个金额都要出现、正文里每个数字都要在证据里有出处、不许内部标识
  且必须带订单号；不合格整轮失败（不降级成模板）。
- `consumer_knowledge.final_answer`：接受可选的 `reply`（仅 `kind=realtime` 且有实时快照时）；
  没有 `reply` 时行为与之前逐字相同，老应用不需要重发。
- `consumer_chat_service.require_result`：模型写的正文无法逐字重算，回读改为用同一套证据校验
  判定（弱一档，已在 ADR 写清代价）。
- `deploy/dify-chat/consumer-service.dsl.yml`：`answer` 节点提示词改成"可以写 reply"的版本，
  含语气要求与数字规则（这份就是编排页里那段 SYSTEM 的仓库副本）。
- README 模块表补 `consumer.consumer_reply.py`；内容锁按新树重建。

## 验证

| 项 | 结果 |
| --- | --- |
| `ruff check .` | 通过 |
| 全量 `pytest -n 4` | **3562 passed / 9 skipped** |
| 本片专项 | 15 条（接受/拒绝各类正文、端到端交付与回读、篡改拒绝、老两字段兼容） |
| 变异验证 | 去掉数字出处校验 → 3 红；回读直接放行 → 1 红 |

## 已知缺口

- **未完成业务验收**：没有在 Dify 编排页改过线上提示词，也没有真实答复样本；
  语气是否更拟人只能人眼看。
- 回读判据弱一档：保留了全部数字的改写会通过（ADR-0187「代价」）。
- 每次 realtime 答复多一次模型调用（此前那条路无模型调用）。

## 下一步

1. 推分支、开 PR、跑确定性 CI、合并、部署。
2. 在 Dify 编排页把 `answer` 节点的 SYSTEM 换成仓库里的新版（或按它改写），
   用真实会话确认正文按新提示词生成、数字与订单号仍在。
3. 管家端的提示词调整独立进行（那条路的正文本来就由模型写，不需要本片）。
