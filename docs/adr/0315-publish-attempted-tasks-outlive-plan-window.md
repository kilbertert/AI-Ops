# 315. 已派发发布任务不受计划墙钟约束，计划窗只约束启动

状态：已采纳；与 ADR-0310/0311/0314 同族无出口缺陷，补发布腿「可启动但
永远完不成」的可行性缺口。

`environment-publish` 任务的首领与首个 `intent` 调用在 `current(fresh=True)`
下复核验证任务的冻结计划与自身计划墙钟——这正确地把「启动」约束在
验证的观察窗内。但 `attempted`（首个 intent 已消费、账本开始记录）之后，
同一条件继续在**每个前向调用**上执行：readiness/capacity/central/target/
verify/activate 各步的 intent、central、connect、source-configuration、
directory 都要 `fresh=True`。真实链路的墙钟是 ~14 分钟量级（readiness 复核 +
capacity 并发窗度量 + 三次业务 trial + 两次远端受管动作 + 每步一次认领周期），
而预检 digest 去重使发布计划与验证计划共享同一个 900s 窗：验证已消耗
5–8 分钟，留给发布链的窗口恒小于链长。

墙钟到期后任务并非终态化，而是进入「可认领但不可前进」：checking 态的
重领走 `current(fresh=False)` 不检查计划，认领照常成功；但每个前向调用
都在 `fresh=True` 复核下被 `ENVIRONMENT_TASK_PLAN_EXPIRED` 拒绝——
任务永远 reconcile、占住互斥闸、无设计出口。env95 第二租户链实测两次
此形之外的更浅失败：发布提交与首领之间计划到期，首领预检直接
blocked（attempted=False、steps={}），证明该窗口连「开始」都常常赶不上。

## 决定

`publication.current` 的计划墙钟闸收窄为只在 `not attempted` 时生效：
启动（首领与首个 readiness intent）仍须发生在计划窗内，首个 intent
消费后 `attempted=True`，后续前向调用只保留取消行与漂移检查
（`fresh and attempted` 分支的草稿 revision / references sha 复核）。
与登记任务的 `dispatched` 先例同构：登记任务派发后
`registration.current` 只按冻结账本对账、不再复核计划与权威；
发布任务 intent 消费后同样只按账本推进，页面取消/恢复仍是设计出口。

## 替代方案

放宽 `validation.current` 在发布首领处的 fresh（允许用过期验证计划
启动发布）：验证窗的「发布须在验证新鲜期内启动」语义本身合理——
阻止拿三天前的验证证据开通；且发布任务的自有计划已证明提交时草稿
一致。放宽它把启动新鲜期也让渡给发布自有窗，收益不抵语义损失。

给发布任务独立延长计划窗（如 publish 专用 TTL）：计划行是共享表与
公开 API，按动作加长 TTL 引入新的契约面；而缺陷本质是「窗绑定完成
而非启动」，不是窗长度不足。

逐步滑动窗口（每个 intent 顺延自己的有效期）：与 attempted 分界
同效但多一套 per-step 时钟机制，复杂度不抵收益。

## 后果

启动在窗内的发布任务，在飞链路不再被 900s 墙钟截成 checking∞；
瞬断/重启后继续按账本推进直至 observed 或页面取消恢复。
`attempted` 前的行为不变：首领与首个 intent 仍要求计划存活，
启动后漂移仍由草稿 revision 与 references sha 复核拒绝，取消行
仍硬拒（PLAN_EXPIRED 错误码照旧承载两因，沿用既有混淆习惯）。
计划窗对「验证后多久内必须开始发布」的约束语义不变。
