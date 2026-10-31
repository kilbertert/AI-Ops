# 固定 32 次真实模型样本：结果、守卫假阳性修复与波动（2026-10-31）

## 结论

用当前代码与线上同一套校验器，**最新一轮 31/32**：

```
total 32 / passed 31 / failed 1
client   15/16
operator 16/16
model: deepseek-v4-flash-0731（生产节点同一出口与密钥）
```

唯一失败 `status/concise` 是**真实模型错误**，守卫按设计拦下：
状态快照没有账单事实，模型却写了 `{{fact:billing_line}}` → `CHATFLOW_FACT_UNKNOWN`。

**不宣称 32/32。** 见下方波动一节。

## 一次真实缺陷：状态守卫的假阳性

32 次样本的价值之一，是它抓到了一个**此前所有单测都没覆盖的守卫缺陷**：

`consumer_facts.check_status_prose` 用「否定词 × 状态词」单维判据，把正常追问当成状态改写：

| 被误拒的正文 | 触发原因 |
|---|---|
| `还需要对照原账单才能判断有没有异常。` | `有没有`（A-不-A）含 `没有`，`异常` 是状态词 |
| `你是不是想核对订单对应的充电站？` | `是不是` 含 `不`，`充电站` 含 `充电` |

修法是**收窄判据而不是删掉判据**：

1. 先剥 A-不-A 疑问式（`有没有/是不是/能不能/会不会/可不可以/要不要/对不对/行不行/是否`）；
2. 状态词匹配前先剥名词化的场所/设备词（`充电站/充电桩/充电枪/充电线/充电口/充电卡/充电记录/异常单/异常码`）。

回归用例成对存在，**放宽与守紧一起测**：

- `test_e_a_normal_follow_up_with_a_question_form_is_not_a_status_assertion`（4 条正例）
- `test_e_a_real_negation_or_assertion_is_still_refused`（4 条负例：`尚未完结，仍在供电`、`订单没有结束`、`订单已经结束了`、`has not finished and is still charging`）

变异验证：撤掉修复后，4 条正例转红；负例在修复前后都保持拒绝。
`tests/consumer/test_prompt_fact_segments.py` + `tests/operations/test_operator_reply_facts.py`：**37 通过 / 0 失败**。

## 波动：失败不是固定的

同一个执行器（修复后）跑了不同轮次，**失败的样本每轮都不一样**：

| 轮次 | 结果 | 失败项 |
|---|---|---|
| 修复前（预算 2000） | 15/32 | 8 × 空正文（推理烧空预算）、7 × evidence 形状错误（执行器缺陷） |
| 修复后 #1 | 30/32 | `diagnose_order/warm`、`site_no_match/warm`（均为守卫假阳性） |
| 修复后 #2 | 30/32 | `billing/warm`（真实违规：正文承诺"已经查到了"）、`consult_howto/warm`（真实违规：漏 `{{fact:order_facts}}`） |
| 修复后 #3（当前代码，串行） | **31/32** | `status/concise`（真实违规：引用了本轮不存在的事实片段） |

**含义**：模型输出有随机性，单轮 32/32 或 31/32 都不能代表稳定性。
真实观察到的**稳定失败模式**只有一类：模型偶尔写出不在本轮证据里的事实片段/支付承诺，
守卫每次都能拦住（这是期望行为，不是缺陷）。

被修复的守卫假阳性是唯一一类**不该出现的失败**，现已连同回归用例关闭。

## 执行器本身修掉的两处（非模型问题）

| 现象 | 真因 |
|---|---|
| 8 条正文为空 | `max_tokens=2000` 被 `reasoning_tokens` 吃光；现默认 8000（`AIOPS_SAMPLE_MAX_TOKENS` 可覆盖） |
| 7 条 `OPERATOR_EVIDENCE_MISSING` | `evidence` 必须**以工具名为键**且含 `cpms_order_detail`；工具名是 `cpms_site_search` 不是 `cpms_sites` |

## ⚠️ 仍未确认的风险：线上 `max_tokens`

- 该模型先花 reasoning token；本次观测推理峰值 **3194**，completion 峰值 3459。
- 生产环境中我**没有找到**显式 `max_tokens`：节点 `exec-manifest.json` 的 `model_options` 是 `{}`，
  节点 `application.yml` 未设，网关客户端路径未设 —— 实际生效值由上游默认决定，仓库内不可核实。
- 若默认值低于约 3200，重推理场景会"正文为空 → 整轮失败"，正是我用 2000 时 8/32 失败的形态。
- 生产 `chat_dify_runs` 150 条中 12 条失败（8 × `CHATFLOW_ANSWER_INVALID` 等），但
  `error_message` 为空，**无法区分**是预算耗尽还是内容不合规。不下结论。

建议（另开任务）：抓一次真实上游请求读出实际 `max_tokens`；若低于推理峰值，把预算显式写进
`exec-manifest.json` 的 `model_options.max_tokens`（`ChatTuning` 已支持，只是没配）。

## 边界

- 31/32 只代表这批固定样本；**不代表长期准确率**。
- 本样本直接调用模型出口 + 线上校验器，**未经过 Dify 应用与页面**。
  页面级证据见 `2026-10-31-prompt-managed-ui-acceptance.md`（117/117，合成模型）。
- **41 原应用 / 原 H5 的真实模型验收仍未执行。**
