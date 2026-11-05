# 222. 授权闸拒绝把原始码写进 run.reason：归因不能依赖被闸反杀的上报

日期：2026-11-05。

Status: Accepted（PRD #1380 第一步，用户确认方案后实施）。

ADR 0221 让节点以 `tool_refused:<原始码>` 上报终局归因。41 实测 F4a
暴露其盲区：运行中换版/撤授权时，**失败上报与授权判定共用 `_current`
一道闸**——闸拒执行回调，也闸拒「汇报自己怎么死的」，节点上报永远到
不了库，`run.reason` 只能落兜底 `operator_failed`，真实码只剩在节点
容器日志里。

决定：闸上拒绝在抛出点就地归因。`_current` 拆为检查本体
`_check_current` 与薄包装：抛 `OperatorRefused` 且 run 仍
`issued/running` 时 `fail(run_id, tool_refused:<CODE>)` 并记
`aiops.operator` warning。`fail()` 按状态窗口幂等——**首写胜出**，
后续闸拒与死 run 上的重试都不改写已落码；stamp 动作 try/except 隔离，
归因失败不改变拒绝形状。

两类不 stamp：`OPERATOR_TOOL_DISABLED`（上报不带 tool，本就触不到
带 tool 的判定，节点自归因路径仍可用）与 `FEEDBACK_REFUSALS`（回喂
业务码，全部抛在闸外，包装器见不到）。这是 0221 分流原则的同一刀：
归因责任落在「做出终止决定的一方」，而不是寄希望于一个可能永远发不
出去的上报。

衍生不变量：`run.reason` 首写胜出——闸上 stamp、节点上报照录、
settle 的 `retracted_by_operator`、worker 兜底四者都是单写者，由
`fail()`/`revoke()` 的 state 窗口保证幂等。run 带码终局后
`_untouched` 为 false，确定性失败不再按模型噪声空耗 resample；
`_raise_node_failure` 已把 `tool_refused:*` 映射
`OPERATOR_DEPENDENCY_UNAVAILABLE`。

## 备选方案

- 放开 `_current` 让失败上报过闸：上报一旦被放行，授权已死的成功
  答复也随之放行——正是 PRD #1380 第二步要拆的混淆，但那不是本补丁
  的最小面；先 stamp 保证「即使后续重构延期，归因也不再丢」，否决。
- 节点侧补偿（离线记日志或重试上报）：节点是唯一不能信任归因可达
  的一侧，且重试在无界窗口下仍是尽务而非保证，否决。
- stamp 所有拒绝码含 `TOOL_DISABLED`：该码现有自归因路径已实证可用
  （上报不带 tool 不过带 tool 判定），stamp 会抢先终结 run 反而挡住
  一次本可成功的上报，否决。
