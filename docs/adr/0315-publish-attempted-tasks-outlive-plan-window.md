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

同一计划墙钟还有第二个传导面：`central_bundle` 把
`request["plan"]["expires_at"]` 原样冻结进发行物 release.json，而远端
setup/stage/activate 与 trial 加载各自复核 `document["expires_at"]`。
即使任务侧放行，一个启动于计划窗内、收尾于窗外的发布会把一份
「签发即过期」的发行物送到远端，死在第一个远端闸门。

卡面是第三个传导面：`_public` 把 `queued` 且计划到期的任务降格显示为
`expired`。发布是唯一会在认领周期间让出回 waiting 的动作，在飞发布
越过计划窗后谎报「已过期」——页面停止轮询并藏起「恢复本次开通」，
设计出口被卡面移除，与 checking∞ 同害。

## 决定

`publication.current` 的计划墙钟闸收窄为只在 `not attempted` 时生效：
启动（首领与首个 readiness intent）仍须发生在计划窗内，首个 intent
消费后 `attempted=True`，后续前向调用只保留取消行与漂移检查
（`fresh and attempted` 分支的草稿 revision / references sha 复核）。
与登记任务的 `dispatched` 先例同构：登记任务派发后
`registration.current` 只按冻结账本对账、不再复核计划与权威；
发布任务 intent 消费后同样只按账本推进，页面取消/恢复仍是设计出口。

发行物的期限不再继承计划窗：`central_bundle` 按签发时刻铸
`expires_at = now + 3600`——完成配额覆盖链尾（目标 stage → 业务
verify → activate 及其重试），远端各闸门的复核语义不变，变的只是
期限从「计划出生时间」改为「发行物签发时间」起算。远端（受管安装器
代码面）一行不动。

`_public` 的 queued→expired 降格豁免 `attempted`：在飞发布让出周期时
卡面保持 queued，页面继续轮询、取消全程可达；未 attempted 的 queued
任务照常降格（首领本就会被拒，expired 是如实结论）。

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

发行物期限沿用计划窗、只放宽远端复核：远端复核是发行物新鲜度的
唯一防线，放宽等于让无限期旧的发行物可激活；且远端代码在受管
安装器面，改动要 CAS 重钉制品——成本与风险都高于改写签发值。

发行物去掉期限：远端闸门按缺键 fail-closed 拒绝；且「发行物必须有
有效期」本身是正确不变量——一小时前的签发不该在今天还能激活。

## 后果

启动在窗内的发布任务，在飞链路不再被 900s 墙钟截成 checking∞；
瞬断/重启后继续按账本推进直至 observed 或页面取消恢复。
`attempted` 前的行为不变：首领与首个 intent 仍要求计划存活，
启动后漂移仍由草稿 revision 与 references sha 复核拒绝，取消行
仍硬拒（PLAN_EXPIRED 错误码照旧承载两因，沿用既有混淆习惯）。
计划窗对「验证后多久内必须开始发布」的约束语义不变。

发行物有了自己的签发期限后，远端闸门复核的仍是一份有意义的
截止时间（签发后一小时），而不是碰巧残留的计划窗秒数——发行物
只对「签发后是否及时被消费」负责，不对「计划何时生成」负责。
在飞发布让出周期不再在卡面谎报过期，取消/恢复出口全程可达。
