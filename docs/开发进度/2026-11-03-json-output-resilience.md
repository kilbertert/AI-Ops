# 里程碑：上游不吐 JSON 的三道防线

日期：2026-11-03。分支：`fix/operator-json-output-resilience`；基线：`8ca1c0c0`；ADR-0197。

## 一、症状

同一张单（`2116050516255272961`）在判据修复上线后仍有约三成失败。
日志：`operator_answer_rejected reason=json_parse plain_text:1248 / 1313 / 1504`。
`plain_text` 的含义是**开头不是 `{` 也不是围栏** —— 模型写的是正文。

## 二、测量（真实上游，每组 25 次）

脚本对模型出口直接发两轮工具循环，第二轮与生产同形
（system + 用户消息 + 工具定义 + `response_format=json_object`）：

| 条件 | 失败 |
|---|---|
| 基线 | 11/25、9/25、8/25（36–44%） |
| 要求写进 system | 4/25（16%） |
| **要求写进用户消息（首条）** | **2/25（8%）** |
| 要求写进末轮 user 消息 | 3/25、1/25 |
| 首条 + 末轮 | 1/25 |
| 用户消息提醒 + 再要一次 | 0/25 |

**为什么选「用户消息」而不是「system」**：节点对 system 消息集合做**逐字**校验
（`GuardedChatModel.verifySystemMessages`），改 system 要换节点 jar 与执行清单；
而 `SnailDispatcher.question` 正是节点看到的那条用户消息，只改 Python。
两者效果相近（8% vs 16%），前者明显更划算。

## 三、三道防线

1. **提醒**（`JSON_ONLY_REMINDER`）—— 只影响模型少犯错，不改变任何判据。
2. **有界重采样**（`MAX_RESAMPLES = 1`）—— 只在 `_worker` 里重新派发。
   必须在这里：`_complete` 是节点回调，拒绝后节点**不重试**（已核对
   `ReportingChatSessionRuntime.reportCompleted`）。
   只在「这一轮没领过工具」时重派，避免重复取证与旧轮次交错。
3. **失败原因透传** —— 节点一直在发 `model_failed` / `model_stream_incomplete`，
   网关此前整个丢掉。额度耗尽那次（403 `insufficient_user_quota`）就是因此
   与「模型写错 JSON」完全同形。

## 四、验证

- 新增 3 条：提醒真的到达实例的 `question`；纯文本答案最多重派
  `1 + MAX_RESAMPLES` 次后按失败交付；节点 `reason` 进日志不再是裸码。
- 写第一版测试时踩到 `SnailDispatcher.question` 是**类属性**：读类属性会拿回默认提问，
  测试永远看不到真正发出去的内容。已改成读实例，注释留在测试里。
- 全量 **3995 passed / 9 skipped**（基线 3987，新增 8 条断言）。
- 镜像内容锁按 `local-gateway-build.md` 真实重建，`context_digest.py --check` 一致。

## 里程碑信息

目标：把「模型最后一轮不吐 JSON」的概率性失败压到用户看不见的量级，并让上游失约有据可查。
当前分支 `fix/operator-json-output-resilience`，PR 见提交；**未部署**。
下一步：合并 → CD 部署 → 用同一张单复测成功率，与本次 8%/0% 的估计对照。

## 五、边界

- **未部署**。生产仍是 `1.0.17+24184f75b224`。
- 8% / 0% 是小样本估计（每组 25 次），不是保证；部署后要按同一张单复测。
- 额度耗尽那次的错误码区分只做到「节点 reason 透传 + 一类依赖失败码」；
  上游 `insufficient_user_quota` 的**正文**在节点侧，网关看不到，因此
  「额度不足」与「模型拒答」目前仍归在同一类 reason 下。这是已知缺口。
