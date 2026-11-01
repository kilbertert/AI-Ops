# 订单实时链路：10/10 通过（2026-11-01）

## 结果

同一真实用户会话、同一真实订单 `2109928861763776513` 连续 10 次查询：
**10/10 completed**，全部落在 `agent_version_no=14`（新发布的协议块），
`reply_source` 是模型写的事实段，`reason` / `reject_detail` 全空。

证据与比对基线见 `docs/validation/2026-11-01-order-path-ten-runs.md`。

## 之前的两轮失败与对应修复

| 版本 | 症状 | 修复 |
|---|---|---|
| `833e15b7` 之前 | `invalid document answer`，无法分辨成因 | `#1291`：`reject_detail` 记形态 |
| `56995d3d` | `json_syntax`：JSON 后多一个 `"}`（12 条里 4 条） | `#1294`：`load_selection` 容忍完整对象后的标点尾巴 |
| `56995d3d` 之后 | `CHATFLOW_FACT_UNKNOWN`：状态查询引用了不存在的 `billing_line` | `#1298`：协议块说明"只引用本轮实际给出的段" + Dify 迁移发布 |

关键教训：**改提示词模板不等于生产生效**。`PROTOCOL_BLOCK` 只是模板，
模型读的是 Dify 里保存的文本；#1298 合并部署后仍要迁移草稿并 `/v1/dify/publish`。

## 仍然未完成

- 业务验收整体未完成：41 原应用 / 原 H5 的其余验收项未跑。
- 本次只覆盖订单实时**状态**查询；账单查询与知识问答未纳入连续样本。
- 10 次样本不构成稳定性证明，只能说这段时间未复现。

## 下一步

按 `docs/acceptance/dify-prompt-lifecycle/PROGRESS.md` 继续未通过的验收项；
订单链路若要更强的稳定性证据，需要更长的连续样本与并发场景。
