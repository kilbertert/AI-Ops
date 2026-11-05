# 223. 回调按消息用途分级校验：执行面过全量授权，上报面只过协议闸

日期：2026-11-05。

Status: Accepted（PRD #1380 第二步，用户确认方案后实施）。

ADR 0222 让闸拒就地 stamp `run.reason`，归因不再丢。但它留下一个
证据缺口：授权已死之后，节点的迟到交付物（工具证据、成功答复）仍被
`_current` 一道闸 403——上报内容是「它死前查到了什么」的最后取证，
被闸反杀后快照里什么都没有，事故复盘只剩「拒绝了」三个字。而且
成功上报被 403 时节点侧往往按失败重试，消耗并不更小。

根本混淆：`_current` 同时回答两个不同的问题——「**你是谁**
（令牌、manifest、run 属实）」与「**这次执行还许不许可**（发布、
范围、目标、绑定仍有效）」。对「我要铸造/使用执行能力」这两个问题
都该问；对「我来汇报已完成的取证」只有第一个是安全问题——上报
不改变任何授权状态，伪造身份才是唯一要挡的。

决定：把一道闸拆成两级。

- `authenticate`（协议级）：run 存在且 operation 匹配、绑定在位、
  节点令牌属实为 `expected_node_id`、manifest 身份匹配。所有节点
  回调都过它，且**不 catch**——伪造令牌/mismatch 的「上报」照样硬拒，
  取证面绝不吸收伪造证据。
- `authorize_execution`（执行级）：协议级之上再跑原 `_current` 的
  全量判定（task/qa 存活、still_authorized、targets、配置四元组、
  节点 active、scope 指纹、`RESULT_RETRACTED`、tool 开关、到期）。
  只包铸造/使用执行能力的面：领配置、工具授权、grant、站点计划。

上报面（`record`/`complete`）先过 `authenticate`，再 try/except 软查
`_authorize_execution`：被授权拒（区别于协议拒）时先走 0222 的
stamp，然后看 run 状态——仍 `issued/running` 说明死的是授权不是
运行（如 `TOOL_DISABLED` 不终结 run），原样 re-raise；已终局则把
交付物并进快照而非丢弃。

终局写入走 `store.absorb(run_id, patch)`：dict 值一层深合并进
`snapshot_json`，标 `superseded: true`，**不动** state 与 reason
（首写胜出已成立），返回 `delivered: False` 告诉节点「收到了，但没
投递」。已成功 run 不进 absorb——重复 complete 走幂等
`delivered: True`，重复 record 照旧早 ack。死 run 上的
`complete(status=completed)` 跳过答复审核：终局后审核无意义，
原文入库仅供取证。

## 备选方案

- 上报面完全放行不做软查：能收，但丢掉「授权为什么死」的 stamp 时机
  ——0222 的归因依赖闸拒动作本身触发，软查保留触发点又不拦交付物，
  否决。
- 终局后拒绝成功上报、只收失败上报：成功答复恰是取证价值最高的一类
  （它证明授权死前工作已完成），按 status 分拣是人为制造缺口，否决。
- `absorb` 允许改写 `state`/`reason`：终局语义会再次分裂——reason
  首写胜出是 0222 刚建立的不变量，absorb 只并快照不碰状态机，否决。
