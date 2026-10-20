# 57. 整轮完成是独立于领取的判据；同 run 重入在本机拒绝

日期：2026-10-20
状态：已采纳
影响版本：`src/aiops_diagnostics/agent_runs.py`（`complete` / `complete_loop`）、
`src/aiops_diagnostics/gateway_api.py`（`is_releasable` / `deny_release` /
`POST …/completions/{run_id}`）、
`tools/m2_close/node/src/main/java/com/trendpower/aiops/node/`
（`RunExecution`、`ReportingChatSessionRuntime`、`LoopTerminationGuard`、`GatewayClient`）

## 背景

M3 的一个 run 走完「签发 → 平台派发 → 节点领取业务 → 模型工具循环 → 结果回执」。
在引入执行配置身份之前，`complete()`（保存业务快照）就把记录写成 `succeeded`。于是
**「工具领取成功」被当成了「整轮模型工具循环完成」**：一次中途崩溃、末轮截断或订阅方
放弃，库里都留下一条 `succeeded`。

两件事在实测里同时发生：

1. 末轮被截断（SSE 无 `[DONE]`、无 `finish_reason`）时，框架照样调用
   `ChatSessionRequest.completionConsumer`，completion 文本是半截，`finishReason` 是
   `_UNKNOWN`。
2. 受控工具失败时，`ToolExecutionException` 被框架默认的
   `DefaultToolExecutionExceptionProcessor(alwaysThrow=false)` 转成文本继续跑，
   模型再回答一次，整轮"成功"结束。

也就是说，「怎么结束的」有两种以上，而「结束了」这个回调本身不区分它们。

## 决定

### 一、用主状态表达「已取数、未完成」，不加新列

启用清单的 run 在 `complete()` 里**只写快照、保持 `running`**；转 `succeeded` 只能由
`complete_loop()` 完成，条件是同一写事务内同时成立：身份逐字相等、仍在 `running`、
已有快照、未过期。取消与进程退出没有回调 ⇒ 停在 `running`，由既有 TTL 收敛为 `expired`。
**`running` + 快照永远不会被读成成功。**

未启用清单的 run（及迁移前历史行）走既有语义 `running → succeeded`，回执键集合不变。

### 二、整轮完成的两个必要条件，缺一 fail closed

- 最后一轮模型的 `finishReason` 必须是 `STOP`（`_UNKNOWN`/`LENGTH`/`CONTENT_FILTER`/
  `TOOL_CALLS`/缺失一律拒绝）；
- Gateway 的完成回执必须写明**本 run** 且 `state=succeeded`（`200 {}`、错 run、
  `state=failed` 都不算确认）。

### 三、成功与失败同一写事务、同一 TTL 原语

失败通知与成功走同一条 `_expire_in` + 事务内 `expires_at <= now`，所以到期之后来的失败
通知不会把 `expired`/`ttl_elapsed` 覆盖成 `model_failed`。同状态同原因幂等，同状态不同
原因 ⇒ 409；已确认的记录不被迟到通知改写；未启用记录一个列都不改。

成功通知的 `reason` 省略与 JSON `null` 都映射到无原因。比较时保留这个值，不将它当成
通配符；因此 `null → null` 幂等，`model_stop → null` 与反方向均为冲突。沿用本地
已声明的同原因契约及请求模型的可选字段语义；未增加 HTTP 幂等键或新的协议。

### 四、同 run 重入在本机拒绝，而不是靠「是否领到」判断

Gateway 对 `running` + 已有快照的**重试会回 200 并重放快照**，所以同一 run 的第二个
执行也能领到业务；若让两个执行都发终局，后失败的那个会把正在正常跑的原 run 杀掉。
因此本机维护一张 active-run 登记表，`begin()` 用 `putIfAbsent` 判定，重复者只回本地
`DuplicateRunRefused`、**不发任何通知**。

登记在**真实终局回调**（completion/error）里移除，不在 `execute` 返回时移除 ——
实测 `executeStream` 在 `Flux.subscribe(...)` 之后立即 return，流仍在跑。

守卫（`LoopTerminationGuard`）在**调用线程**上交接：框架在 subscribe 之前同步构建
ChatClient，所以同一 run 的两次执行各持自己的守卫，不存在「后者的 attach 落到前者的槽里」。

## 被否的方案

- **加一列 `loop_state`**（草案）：它把「哪种成功」变成第二个状态机，而主状态已经能表达
  「已取数但未完成」（`running` + 快照）。多一列就多一处需要与 `state` 保持一致的判据。
- **用「是否领到业务」判断归属**：Gateway 的 running+快照重试会 200，两个执行都会领到，
  判据不成立。
- **在 `execute` 返回时移除登记**：`executeStream` 订阅后立即返回，流还在跑；按返回移除
  等于把并发窗口重新打开（这正是 R1 复现的缺陷）。
- **给取消造一个信号**：框架不提供（completion/error 两个回调都不到）。伪造一个就等于
  把「进程被杀」写成「正常结束」。现在的做法是承认边界：15 分钟（大于 Gateway 的 180 秒
  TTL）清扫 + 256 硬上限，满表时拒绝新执行，而不是无界增长。
- **把并发判据塞进真 Gateway 的端到端验收**：那一层每次派发只起一个节点进程，结构上
  覆盖不到同 JVM 两个 run；强行塞入就得伪造 run 记录或加第二个编排路径。改为独立的
  `m3link/ConcurrentRunAcceptance`（自带替身 Gateway，故意对任何 run 都回 200）。

## 后果

- 撤销、到期与失败都是**真实终局**：节点不可能在之后再把这条 run 确认成功。
- 一次被取消的 run 在 15 分钟内仍占一个登记名额 —— 这是**已知且写明**的边界，不是
  被隐藏的保证。
- 该端点不返回任何业务数据；它回答的是「这条已核对的 run 走完没有」，不是「再给我一次快照」。
- **完成通知是受信声明，不是执行证明。** 持有节点凭据的一方可以在领取快照之后直接发
  `status=completed`，而 Gateway 无法验证「模型工具循环真的跑完了」——它看不到那个循环。
  这不是本片的缺口，而是**凭据本身就是信任边界**：同一个节点本来就可以不领取、不调工具，
  或者什么都不发。所以这里能做的只有两件事，且都做了：把声明**如实记录**（`reason`、
  `completed_at`、`observed_agent_version`），以及在节点侧用「末轮 `finishReason=STOP`
  + Gateway 回执确认」两道判据把「误报」挡住（`LoopTerminationGuard` 与
  `ReportingChatSessionRuntime`）。**恶意节点的伪造不在可防范围内**，不要把它写成已防住。
- 成功通知也可以带 `reason`：同原因幂等、不同原因 `conflict`。早先 `completed` 的
  `reason` 被丢弃，于是两条不同原因的成功通知都返回 200 —— 与失败路径上的规则不一致。
