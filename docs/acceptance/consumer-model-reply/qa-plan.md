# 编排页提示词写正文 QA 计划

契约见同目录 [acceptance.feature](acceptance.feature)，取舍见
[ADR-0187](../../adr/0187-consumer-chatflow-prompt-writes-the-reply.md)。

## 自动化用例（本片已实现并跑过）

| ID | 环境 | 前置 | 数据 | 动作 | 预期可观测结果 | 清理 |
|---|---|---|---|---|---|---|
| MR-01 | pytest | 快照含 2.4/2.40/0 与订单号 | 合格 reply | `validate_reply` | 返回同一段正文 | 无 |
| MR-02 | pytest | 同上 | 漏 `recorded_refund_amount` | 同上 | 抛错，错误含字段名 | 无 |
| MR-03 | pytest | 同上 | 编一个 2.5 | 同上 | 抛错，错误含 `not in the evidence` | 无 |
| MR-04 | pytest | 同上 | 编"约 47 分钟" | 同上 | 同上 | 无 |
| MR-05 | pytest | 同上 | 含 `cpms.`／`status_code=` | 同上 | 抛错，错误含 `internal identifiers` | 无 |
| MR-06 | pytest | 同上 | 缺订单号 | 同上 | 抛错，错误含 `order number` | 无 |
| MR-07 | pytest | 同上 | 空串／2000 字以上 | 同上 | 抛错，错误含 `empty`／`too long` | 无 |
| MR-08 | pytest | 同上 | "共 3 项" | 同上 | 通过 | 无 |
| MR-09 | pytest + 网关 | chatflow 桩带 reply | 合格正文 | 提问 → 回读 | 交付该正文；回读 200 且正文不变 | 临时库 |
| MR-10 | pytest + 网关 | 同上 | 库里正文改成编了数字的版本 | 回读 | 非 200，且不返回改动后的正文 | 临时库 |
| MR-11 | pytest | 无 live 快照 | 带 reply 的答复 | `final_answer` | 抛错 `reply without live data` | 无 |
| MR-12 | pytest | live 快照 | 只有 kind/faq_id | `final_answer` | 走确定性渲染，正文含金额 | 无 |

## 真实环境验收（本轮不做，不得记作通过）

- 在 Dify 编排页把 `answer` 节点的 system 提示词换成仓库里的新版本，用真实会话确认
  正文按新提示词生成、数字与订单号仍在。
- 需要一次真实账号提问；改线上提示词属于运营动作，本轮没有做。
- 真机观感（语气是否真的更拟人）只能人眼判断。
