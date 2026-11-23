# 321. 未派发安装任务的过期/取消出口按 dispatched 分界而非 attempted

状态：已采纳；与 ADR-0308/0314/0315 同族的认领循环无出口缺陷，补
`candidate-install` 腿的分界错误。

`candidate-install` 的 `attempted` 只证明三件事：导出目录已建、
`candidate.tar.gz`/`runtime.tar.gz` 已按冻结计划导出、归档清单已写进
request。它**不**证明远端曾越过任何副作用——真正的派发授权是
`authorize` 入口把 `dispatched` 翻转为 true，该翻转严格发生在 worker 向
目标宿主发送 `run` 动作之前。

缺陷链条：`current()` 的取消/计划窗口检查挂在 `fresh`（即 `not
attempted`）之下。一旦 `attempted=True` 而 `dispatched` 未翻转——worker
死在传输与 authorize 之间、租约瞬断、HTTP 丢弃——认领循环对每个周期都
用 `fresh=False` 放行，stage 又回 `observe_only=attempted=True`，于是
worker 永远只跑远端 `observe`：95 号现场无 `installation-tasks/<id>` 意图、
无 pending，远端 observe 只能回 blocked/异常骨架，任务在
checking↔executing 间无限循环，BUSY 互斥同时挡住同环境新任务的提交。
页面取消同样破不了环——`row["cancel"]` 只在 `fresh` 分支检查。

`non-dispatch ≠ attempted` 的语义**无法从 attempted 重建**：两个标志落在
不同的落盘点（导出=文件系统+request 字段，派发=authorize 事务里的
request 字段），中间隔着 worker 的传输、文件复制与 authorize 调用，任何
一段中断都会留下 `attempted=True`+无 `dispatched` 的状态。把 attempted
当作「可能已 apply」既会把零副作用任务永久楔死，又会把可安全重试的窗口
误报为只能观察。

## 决定

`current()` 的取消/计划窗口闸从 `fresh` 放宽为 `fresh or not dispatched`：
- 未派发任务（无 `dispatched`）无论是否 attempted，过期或取消一律拒
  绝——认领循环将其终态化（取消且未派发落 `cancelled`，其余落
  `blocked`，与 ADR-0314 登记 absent 出口的 cancelled/blocked 配对
  同规），BUSY 释放。已派发行即使带取消也只落 `blocked`：cancelled
  终态需要「远端零副作用」证明，dispatched 已翻转后不再具备。
- 已派发任务保留冻结对账豁免：`fresh=False` 时仍跳过窗口/取消检查，
  只对账远端真实状态，绝不因权威不新鲜而重放 apply。
- 父终态、bindings、node_binding 等漂移检查对两类任务一律保留。

stage 的 `observe_only` 分界从 `attempted` 改为 `dispatched`：
- `dispatched` 未翻转 ⇒ 远端可证零 apply（authorize 是 `run` 的唯一
  前置），窗口内允许带冻结归档重试 `run`+authorize，任务因此能完成
  真实安装而不是永远观察。
- `dispatched=True` ⇒ 远端可能已有副作用，只能 `observe` 回收结果。
- 输出幂等（attempted：归档文件存在即不重导）与执行幂等
  （dispatched：翻转一次即不再 run）自此各归各位。
- `site-install`（cold）的 `observe_only` 沿用 `attempted` 语义不变；
  trial/publication/registration 不动。

## 替代方案

对未派发任务像登记腿那样加远端意图探针
（`installation-tasks/<id>.json`）：无必要——`dispatched` 在 authorize
翻转先于远端执行已是充分的零副作用证明，探针只会增加一次 dev-host
往返而没有新信息；远端 task.py 自身的「意图在场⇒observe」兜底也使误发
`run` 不可能越过二次 apply。

把 `observe_only` 维持在 `attempted` 并只修过期闸：过期循环虽止，但
「attempted+未派发+窗口有效」的任务仍只能观察——远端无意图时 observe
回 blocked，任务被误终态化而没有真正安装，等于把可恢复楔形换成
静默失败。

允许 authorize 二次派发（re-arm）：`dispatched` 单调递增是「最多一次
apply」的执行幂等不变量，放宽它需要新的远端证明面，收益不抵风险。

把取消/过期检查提到 `tasks._current` 的 reconciling 分支：会波及
publication/validation/registration/cold 四条腿的既有冻结合同，违反
本次范围边界。

## 后果

计划过期或页面取消的未派发安装任务在下一个认领周期终态化
（blocked/cancelled），不再占用 BUSY；95 号现场楔死的任务正是此形。
窗口内 `attempted`+未派发的任务改为可重试安装，authorize 后翻转为
dispatched 并转入观察恢复。已派发任务的冻结对账、远端 pending 的
RECOVERY_REQUIRED 核查 hold、以及同 scope/refs 的漂移拒绝全部不变。
旧 request 无 `dispatched` 键时 `request.get("dispatched")` 按未派发
收敛，向后兼容。
