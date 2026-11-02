# 管家输出契约措辞调整的验证（2026-11-02）

分支 `fix/operator-reply-prompt-digits`，基线 `1538237825f1`（`origin/main`）。
决策见 [ADR-0193](../adr/0193-fee-fields-and-order-reference.md) 第三节。

> **本记录只覆盖措辞改动、单测与改前基线；改后成功率未测。**
> 「部署了」不等于「修好了」，这条不合并。

## 一、缺陷是怎么发现的

`#1305`（费用字段进答复）部署后，用**真实运营账号**做端到端验收
（方法见 [`docs/agents/operator-e2e-real-account.md`](../agents/operator-e2e-real-account.md)）。
第一轮通过，随后同一句原话反复跑时开始失败：

```
POST /v1/assistant/questions
→ 202 queued → completed      （第 1 次）
→ 202 queued → failed         （第 2、3 次）
```

网关日志给出了原因：

```
WARNING aiops.operator operator_answer_rejected reason=unsupported_fact
WARNING aiops.operator operator_completion_refused code=OPERATOR_INVALID_ANSWER
```

`unsupported_fact` 来自 `_validate_reply_facts`：`reply` 里除
`{{fact:order_facts}}` 那一行外的每一行都要过 `consumer_facts.check_notice`，
而它**禁止出现任何数字**（防模型凭空写金额）。

本地复现条件（`check_notice` 直接调用）：

| 模型可能写的开场 | 结果 |
|---|---|
| `我帮你核对了本次记录。` | OK |
| `我核对了启动费用、优惠金额和增值税。` | OK（无数字） |
| `建议先核对启动费用与增值税比例为 2% 的那一项。` | **FAIL** `notice states a number` |
| `这笔订单共有 4 项费用需要核对。` | **FAIL** |

## 二、根因

`#1305` 把费用字段**枚举**进了「系统会在这里显示…」那句：

```
系统会在这里显示单号、状态、费用区全部金额（是否预付、预付费、电费、…）
```

紧接着是「不要在其它行复述这些值」。两句话语义打架：模型把枚举读成"这些是可以谈的"，
开场就带金额或百分比 → 被 `check_notice` 整轮拒绝。

**这不是 `check_notice` 的缺陷。** 它挡的是真实风险（模型凭空写金额），放宽它等于
放弃 ADR-0190 的分工（系统写事实、模型写语气）。

## 三、改法

把禁令写死并提高优先级：

```
reply的其它行一个字都不要带数字（金额、百分比、条数、时间都算），
状态断言和支付结果同样禁止，即使抄对也会拒绝交付。
那里只写语气、核对建议与追问，不要提具体是哪几项费用。
```

示例仍是纯语气句（`我帮你核对了本次记录。` / `建议对照原账单，你想先核对哪一项？`）。

## 四、改前基线（**已测，6 次**）

同一句 `帮我分析这笔单的电费2116050516255272961`，旧契约下连跑 6 次：

| 结果 | 次数 |
|---|---|
| `completed` | **4** |
| `failed` | **2** |

**约 67% 成功率。** 这比先前只看 3 次得到的印象（1/3）更可信 —— 3 次的样本太薄。
这条本身也是个教训：**概率性缺陷的基线至少要 6 次**。

## 五、验证

```
uv run pytest tests/operations tests/execution        → 624 passed, 2 skipped
uv run ruff check …  /  ruff format --check …         → 通过
```

新增 `test_output_contract_tells_the_model_the_fact_block_carries_every_fee`：同时断言
「枚举里有费用项」与「有数字禁令」。只满足一半就是这次的缺陷 —— `#1305` 只满足了前一半。

## 六、未做

- **改后成功率未测。** 提示词冻结在每次运行签发的清单里（实测：最近两次运行的
  `execution_manifest.system_messages` 都是旧文本），所以必须部署 Gateway **并重新发布**
  才生效。未部署、未发布、未连跑。
- 没有放宽 `check_notice`，也没有改 `_validate_reply_facts` 的判据。
