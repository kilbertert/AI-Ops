# C端答复"先说结论"QA 计划

契约见同目录 [acceptance.feature](acceptance.feature)。实现取舍见
[ADR-0181](../../adr/0181-consumer-answers-lead-with-the-conclusion.md)。

## 自动化用例（可实现、可复跑）

| ID | 环境 | 前置 | 数据 | 动作 | 预期可观测结果 | 清理 |
|---|---|---|---|---|---|---|
| CP-01 | 本机 pytest | 干净 worktree | `order_amount=27.36`、`recorded_payment_amount=0.00`、`refund_status=0` | `billing_answer(snapshot, "zh")` | 第一段同时含 `27.36` 与 `0.00`，且含"没有退款" | 无 |
| CP-02 | 本机 pytest | 同上 | `status_role=finished` | `status_answer(snapshot, "zh")` | 第一段含"充电结束"，且不以字段名开头 | 无 |
| CP-03 | 本机 pytest | 同上 | `recorded_refund_amount=None` | `billing_answer(...)` | 正文说明"没有记录"，且全文不含凭空的金额 | 无 |
| CP-04 | 本机 pytest | 同上 | `recorded_payment_amount=0.00` | `billing_answer(...)` | 含"不等于"式限定；不含"已扣款""支付成功" | 无 |
| CP-05 | 本机 pytest | 同上 | `source=cpms.orderDetail`／`cpms.chargingInfo` | 两条渲染 | 出现"订单账务记录"／"充电状态记录"；全文不含 `cpms.`、不含 `status_code=` | 无 |
| CP-06 | 本机 pytest | 同上 | 全部金额字段为空 | 两条渲染 | 每项都说明"上游没有提供"，不留空值 | 无 |
| CP-07 | 本机 pytest | 同上 | 十语种 × 两种答复 | `status_answer(..., localized=True)`／`billing_answer(...)` | 每种语言第一段是该语言结论句；订单号与 `YYYY-MM-DD HH:MM:SS UTC` 仍在；无微秒 ISO | 无 |
| CP-08 | 本机 pytest | 同上 | 十语种语言包 | `test_fixed_copy_preserves_placeholders_*` | `status`／`billing` 的占位符集合逐语种相同，且新键齐全 | 无 |
| CP-09 | 本机 pytest | 同上 | 账单答复 | 读正文 | 免责语义仍在、声明未发起付款或退款 | 无 |
| CP-10 | 本机 pytest + 网关 | 无该单可见范围 | 他人订单号 | `GET /v1/assistant/questions/{qa_id}` | 与原判据相同的拒绝码，正文无任何订单字段 | 无 |

## 真实环境验收（本轮不做，不得记作通过）

- 在真实账号与原 H5 上看两种答复的实际渲染（换行、`·` 项目符号、第一段是否真的在第一屏）。
- 需要一次正常登录与一张本人订单；本轮没有做，因此本片**未完成业务验收**。
- 部署瞬间回读影响：见 ADR-0181「代价」一节，需在部署前决定是否接受 180 秒窗口。
