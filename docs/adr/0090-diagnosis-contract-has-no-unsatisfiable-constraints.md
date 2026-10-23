# 90. 诊断交付合同不得包含无解的约束

日期：2026-10-22
状态：已采纳
影响版本：`src/aiops_diagnostics/agent_validator.py`（`AgentResultValidator`）、
`src/aiops_diagnostics/agent_engine.py`（`_finish_success`）、
`src/aiops_diagnostics/agent_contracts.py`（`Hypothesis` / `AgentDiagnosis` 字段说明）

## 背景：一个订单诊断要 2–3 分钟，而工具只占 0.4 秒

2026-10-22，用户在 41 上用 13333555555 实测「帮我查询订单 2110544580381650945」，
拿到正确响应但耗时数分钟。逐轮拆解 `run-20261022T104113Z-50db2c7b-6452`：

```
10:41:15  第1轮 commandcode → 429，失败        2.7s
10:41:20  切到 baoyun，第1轮完成              24.4s
10:41:44  工具 order_snapshot                 0.1s
10:41:44  第2轮 → 要 fee+device+runbook       38.6s
10:42:23  工具                                 0.4s
10:42:23  第3轮 写出诊断 → 校验失败            70.0s   ← 最贵
10:43:33  第4轮 修合约                        15.4s   校验又失败
10:43:48  第5轮 再修                           8.8s
10:43:57  第6轮 补一条工具 → 完成              18.6s
```

跨 128 轮、2196 秒的统计：中位 9s、p90 41s、最大 142s；工具每次 0.1–0.4s。
**时间在模型轮次上，不在数据面。** 而 6 轮里有 3 轮是纯粹的合约返工。

`validation_attempts` 在全部已结束 run 上的分布：

```
{0: 3, 1: 7, 2: 44, 3: 9}      # 44/63 卡在默认上限 2
```

这不是偶发：**44 个 run 撞满重试上限**。

## 三个成因，其中两个是「无解约束」

### 一、`failed_sources` 的判据自相矛盾

journal 里的失败源是 `tdengine:charging-gun_property`。模型据实写成

```
tdengine:charging-gun_property（查询被拒：Invalid column name: batteryMinTemperature）
```

旧判据按**整串相等**比较，于是**同一次响应**得到两条互相否定的报错：

```
结果遗漏失败数据源: tdengine:charging-gun_property
结果声明了日志中不存在的失败数据源: tdengine:charging-gun_property（查询被拒：...）
```

不写解释 → 判「遗漏」；写了解释 → 判「不存在」。**这对规则合起来没有解。**

而且它必然被触发：`batteryMinTemperature` 列缺失、`charging-pile_comm` 表不存在都是
**环境预检已知的缺口**，每轮诊断都会写进 next_prompt。

### 二、假设必须引用「成功」证据

旧规则要求每个 `hypothesis` 引用一条**非 runbook 的成功**证据。于是
「这个通道不可用」这类假设永远过不了 —— 它陈述的正是失败，能引用的只有那条失败条目。

41 上两轮实测（`run-20261022T092735Z`、`run-20261022T104113Z`）：

```
假设“环境数据面缺口导致遥测核对不可用”没有引用非 known_runbook 的直接成功证据
```

约束的**本意**写在 `diagnosed` 那一句上：结论不能只挂在 runbook 上。失败条目同样是
真实观测，恰恰是这类结论的直接依据。保留「不能只引 runbook」，去掉「必须成功」。

### 三、两个最常出错的字段在 schema 里没有说明

`hypotheses[].evidence_ids` 必填且非空，`failed_sources` 要求源码标识符 ——
但下发给模型的 strict schema 里**两者都没有 description**。模型只能靠撞墙学会，
而撞墙的价格是一整轮（p90 41s）。

## 决策

1. **`failed_sources` 按「已知源码 + 非标识符字符」匹配。** 源码是标识符，后面接的是
   给人读的解释，不参与比对。`tdengine:charging-gun` 与
   `tdengine:charging-gun_property_x` 仍然是「不存在」—— 放宽的是「可以带解释」，
   不是「可以只写个开头」。不能反过来从声明里切源码：解释自身带冒号
   （`Invalid column name: ...`），切出来会把解释混进标识符。
2. **假设的 evidence 判据从「成功」改为「真实」。** runbook 仍然不算。
3. **验证放宽，落库仍收紧。** `result` 会被整体存进诊断行并原样出现在
   `/v1/standard/diagnoses/{id}` 的 `result` 里，所以 `_finish_success` 用同一套
   `_declares` 把 `failed_sources` 规范化回源码本身。解释不丢：存在失败源时校验器
   强制要求 `limitations`，模型已经把原因写在那里。
4. **把道理写进 schema 的 description。** 模型看不到 Python 类型注解。

## 备选方案（都考虑过，都没采用）

1. **调大 `max_turns` / `max_validation_retries`。** 不采用：44 个卡在上限的 run 里
   多数**本来就产出了正确答案**，它们缺的不是预算，是一个能通过的判据。加预算只会让
   注定失败的 run 花更久才失败，并把 p90 推得更高。
2. **只改提示词，让模型「照抄源码、不要加解释」。** 不采用：这是一条**模型可以违反**
   的请求，而违反的代价正是那两轮返工。校验器是确定性边界，边界的错要用边界修。
   （提示词仍然改了 —— 作为 schema description，见决策 4 —— 但那是减少撞墙，不是判据。）
3. **把 `failed_sources` 整个放宽成自由文本。** 不采用：它会变成「模型说什么就是什么」，
   而这个字段的全部价值就是能和 journal 的 `source` 对上。放宽的只是「可以带解释」。
4. **失败时就返回 blocked，不做诊断。** 不采用：环境预检已知的列缺失/表缺失是
   **本环境的固有缺口**，不是本次故障。因此永久 blocked 等于在这个环境里关掉诊断。
5. **把 `hypotheses` 从必填改成可选。** 不采用：假设是因果结论的可审计载体；去掉它
   换来的是更短的响应，不是更准的响应。改的是「必须引用成功证据」这半条错误规则。

## 为什么不是「调大 max_turns」

那样只是让失败的 run 花更久才失败，且 `validation_attempts=2` 的 44 个 run 里多数
本来就返回了正确答案——它们缺的不是预算，是**一个能过的判据**。

## 没有做的事

- 不改 `max_turns` / `max_tool_calls` / provider 路由。
- 不动授权链路。
- **没有**把「简单查单」从完整诊断里分出来。当前一个查单请求仍然走
  order_snapshot → fee/device → 诊断的完整链路。这是下一个可测量的优化点，
  本 ADR 只消除**确定性返工**，不改变业务语义。

## 验证

- 新增用例逐条复现生产报错文本（红），修后转绿；变异复验：恢复整串相等后转红。
- `docs/validation.md` 记录实测数据与复验方式。
