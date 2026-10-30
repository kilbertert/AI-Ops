# C 端答复改版在 41 上的真机核对（里程碑记录）

分支`docs/consumer-plain-answers-live`，基线`d7ae3042`。
验收证据见
[2026-10-30-consumer-plain-answers-live-verified.md](../validation/2026-10-30-consumer-plain-answers-live-verified.md)，
决策见 [ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)。

## 目标

把 #1175／#1202 的改动**在真实环境上验一遍**：确认用户反馈的那条答复就是它、
确认新正文真的在生产上出现，并纠正上一轮写错的结论。

## 已完成内容

- **确认对象**：41 的 `assistant_questions` 在 `2026-10-30T03:41:13Z`（与反馈截图同一分钟）
  只有一条 `qa_5c85458e…`，返回的正是旧格式（`status_code=1`／`status_role=finished`／
  `cpms.chargingInfo`／微秒 ISO）。用户看到的就是这条面。
- **真机核对通过**：真实会话（租户 `…861824`）+ 本人真实订单 `2107546575906942977`，
  经公网入口 `POST /v1/assistant/questions`，状态与账单两种答复都返回新格式
  （结论先行、人话来源、无内部标识、无微秒）。
- **纠正上一轮的错误结论**：`docs/validation/2026-10-30-consumer-plain-answers-live.md`
  写的「该面在 41 上未启用」是错的。按「一条记录一个文件」的约定，该文件保留原样并加更正横幅，
  通过的证据写进新的 `…-live-verified.md`；交付状态索引改指向新记录。

## 错因（三条，都会重复发生）

1. **读错了配置文件**：开关在 `/etc/aiops-41/gateway.env`（systemd 的 `EnvironmentFile`），
   我去 `production.env` 里找。41 手册 §1.0／§1.2 本来就写明两个文件的分工 ——
   知识在仓库里，是执行时没照手册查。
2. **把端点登记读成功能开关**：`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 只登记了租户
   `1942105476598861824`；我随手挑的会话属于别的租户，拿到 `404 TENANT_ENDPOINT_UNRESOLVED`，
   却按「功能没开」解释。
3. **问法没进那条路由**：「查订单的账单」不含状态词，不进 `is_order_status_question`，
   于是 `409 CHAT_ROUTE_UNAVAILABLE`。

## 验证结果

| 项 | 结果 |
| --- | --- |
| 线上修订 | `1.0.0+0b58d502adc5` |
| 状态答复（真机） | `completed`，正文为「这单当前的状态是「充电结束」。」+ 订单号 + 时间 + 说明 + 「来源：充电状态记录」 |
| 账单答复（真机） | `completed`，正文为结论句 + 三项人话注释 + 免责句 + 订单号 + 「来源：订单账务记录 · 时间」 |
| 内部标识 | 两种答复里都没有 `cpms.*`／`status_code=`／微秒 ISO |

## 已知缺口

- **观感未验**：结论句是否真在第一屏、`·` 与编号在真机上的对齐，需要人眼看截图；
  本轮只验到文本。
- 九种非中文译文**未经母语者复核**。
- 账单答复依赖 chatflow 取到 `cpms.orderDetail` 快照；本轮只覆盖一个租户、一张单。
- 另一条既存事实（非本片引入）：`is_order_status_question` 不认「账单」，
  所以「查订单的账单」这类问法在**没有 chatflow 绑定**的租户上会得到 `409`。

## 下一步

1. 由人或业务同事在真机／原 H5 上截一张图，确认观感（这是唯一还缺的一环）。
2. 非中文语种若真有用户，找母语者复核九种译文。
3. 若要让「查账单」在没有 chatflow 的租户上也能答，需要单独改路由判据 —— 那是行为变更，
   不在本片范围。
