# 上游不吐 JSON：测量、三道防线与验收

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

## 三之二、复审订正（同一 PR 内）

第一版有三处真缺陷，全部在复审中指出并按实测修掉：

1. **重采样在生产路径上根本不会执行**（最严重）。拒绝是**以异常回来的**：网关回 4xx/409
   → 平台那次调用失败 → `SnailDispatcher` 映射成 `AgentRunDispatchError` 抛出 → 直接进
   外层 except，永远走不到状态判断。第一版测试的假派发正常返回，掩盖了这一点。
   现在先接住 `AgentRunDispatchError` 再判能否重派；测试的假派发也照抄生产行为
   （先调完成端点、再抛 dispatch 错误）。已做变异验证：去掉修复后该测试变红。
2. **站点查询重试会带着旧参数**。`grant` 在取证据**之前**就把模型提议的关键词记进
   `task.arguments`，所以「没证据」不等于「没做过事」。重试换了关键词会撞
   `OPERATOR_TOOL_ARGUMENTS_CHANGED`，把一次本来能成功的查询搞死。
   判据补上 `not task.arguments`。
3. **没回调的派发被误报成模型失约**。`OPERATOR_MODEL_OUTPUT_UNSTABLE` 现在只在
   「派发成功、回调走完、run 仍没被推进」时出现 —— 那才是答案被拒。
   派发本身失败时直接抛出，不冒充模型问题。

## 四、验证

- 新增 3 条：提醒真的到达实例的 `question`；纯文本答案最多重派
  `1 + MAX_RESAMPLES` 次后按失败交付；节点 `reason` 进日志不再是裸码。
- 写第一版测试时踩到 `SnailDispatcher.question` 是**类属性**：读类属性会拿回默认提问，
  测试永远看不到真正发出去的内容。已改成读实例，注释留在测试里。
- 全量 **3995 passed / 9 skipped**（基线 3987，新增 8 条断言）。
- 镜像内容锁按 `local-gateway-build.md` 真实重建，`context_digest.py --check` 一致。
- **`verify-aiops-gateway` 独立证据**：一次性数据根启动真实 Gateway，八项检查全过
  （`/health` 只读、问答与管理接口匿名 401、公开 OpenAPI 200、四个退役端点 404），
  证据在 `dist/verification/gateway/aiops-gateway.json`。
  它**不能**验证远端模型行为、节点回调或真实业务准确率 —— 那些要靠部署后的复测。

## 五、边界

- **未部署**。生产仍是 `1.0.17+24184f75b224`。
- 8% / 0% 是小样本估计（每组 25 次），不是保证；部署后要按同一张单复测。
- 额度耗尽那次的错误码区分只做到「节点 reason 透传 + 一类依赖失败码」；
  上游 `insufficient_user_quota` 的**正文**在节点侧，网关看不到，因此
  「额度不足」与「模型拒答」目前仍归在同一类 reason 下。这是已知缺口。
