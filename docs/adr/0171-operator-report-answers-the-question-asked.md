# 171. 管家报告必须回答用户问的那一点，取证字段覆盖金额与额定功率

日期：2026-10-29
状态：已采纳

## 背景

2026-10-29 的测试反馈：同一张订单（`2113549588485718017`）先后问「分析这笔订单的电费」与
「分析这个订单的充电电压」，两轮报告的 `summary`／`probable_cause`／`next_steps`／
`limitations` 几乎逐句相同，只有订单号不同。两轮都说「未提供单价/金额字段，无法核算电费」
与「监测样本 0 条，无法核对电压」。

这是**两个独立缺陷叠在同一个症状上**，只修一个都不够：

1. **输出协议里没有「回答提问」的位置。** `OPERATOR_OUTPUT` 只声明四个业务桶
   （事实概述／可能原因／建议／缺证），而 `_worker` 的派发文本还写着「用户描述仅**供理解**问题」。
   任何问题都只能被概述一遍，模型没有可据以作答的字段。
2. **工具把答案所需的字段丢在了上游。** 公司 `getOrderInfoDetail` 返回的
   `OrderPageResp` 含 `electricityFee` / `serviceFee` / `totalAmount` / `payAmount` /
   `marketAmount` / `refundStatus` / `refundAmount`，`ChOrderInfoModel` 另含 `gunPower` /
   `interfaceType` / `voltage` / `startSoc` / `endSoc`；而 `DETAIL_FIELDS` 当时只挑 13 个
   字段，**一个金额都没有**。

第三条事实是**证据形状**：`iot_power_runtime` 只保留 7 天（公司 `CleanTableDataJob` 每天 03:00
删 7 天前的行），这笔单的枪从未上报过样本；但证据里只有 `truncated=false`，
「一条都没取到」与「取全了」在图上看不出差别，模型只能复述同一句「不是完整时间线」。

## 决策

- 诊断答案增加**必填非空**的 `direct_answer`，并在报告里**放在最前**。它在 Gateway 侧与
  `summary`／`probable_cause` 同一条文字判据：空串即 `OPERATOR_INVALID_ANSWER`。
  于是「没回答提问」是一次明确的拒绝，而不是一份看起来完整的通用报告。
- `DETAIL_FIELDS` 补齐金额、退款、SOC、额定功率与接口类型（两侧同步：Python 投影与
  Java `CpmsReadTool.detail` 必须逐项相同，否则 `record()` 因证据不相等拒绝整轮）。
- 证据增加 `samples_returned` 与 `absence`，让「没上报过样本」与「被截断」分开陈述；
  输出提示词要求 `limitations` 区分「上游未提供」与「本次未取到」。

**纯电价**（`tip/peak/flat/valley*Fee`）在 41 全为 NULL，`platformAmount` 同样全空，
故不加入 —— 加了只会让模型把一列 NULL 当成证据。计费模板的分时段明细
（`ch_fee_template_record.period_fee_detail`，含 `feeRate` 单价）在外层响应的
`periodFeeDetail` 里，本层不取，是**已知缺口**而非已覆盖事实。

## 被否掉的替代形态

- **只改提示词、不加字段。** 实测该订单金额就在响应里；不取就是不取，改措辞只会让模型
  把「未提供」换成「未取到」，仍然不是答案。
- **让模型自己从 `electricity` 与 `time` 推算电费。** 那是让模型生成数字，而单价在
  `periodFeeDetail`、金额在 `totalAmount`——推算出来的数字没有任何可核对的来源。
- **在 Gateway 侧按提问关键词路由到不同报告模板。** 把「回答什么」变成正则表达式，
  与「模型只写散文、数字来自确定性来源」的既有分工相反，且新增关键词就要改代码。

## 验收

- `tests/operations/test_operator_cpms.py::test_different_questions_must_produce_different_answers`：
  同一张单两个问题必须给出不同正文，且正文以各自的 `direct_answer` 开头；并断言
  `electricityFee` / `totalAmount` 真的进了证据。**变异验证**：删掉报告里那一行
  `answer["direct_answer"]`，该测试失败 —— 它覆盖的是缺陷本身，不是字段是否存在。
- `tools/m2_close/m3node/verify_operator_cpms.py` 新增 `unanswered` 用例（空
  `direct_answer` 必须整轮失败），并断言每轮模型请求里都出现 `direct_answer`。
  真 Java 整链实测：`operator_answer_rejected reason=text_field`，
  `CPMS_TOOL_ACCEPTANCE PASS assertions=114`。

本地通过不证明真实供应商会照做；真实账号与订单上的验收仍按
[输出契约QA](../agents/operator-cpms/output-contract-qa.md)执行。既有已发布清单的
`schema` 不变，但**提示词与工具描述都变了**，节点与 Gateway 需同时更新并重新发布。
