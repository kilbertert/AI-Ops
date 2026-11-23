# 312. 消费者候选被发布提升后准备对账放弃继承资源并重铸

状态：已采纳；env95 第二租户验收实测暴露的无出口缺陷，与 ADR-0310/0311 同族
（取消路径缺少恢复判据），但失败面在准备与登记对账一侧。

`central_bundle` 会把消费者候选应用原地提升为正式应用：`service_keys.ensure`
建 API key、`publish_workflow` 写入已发布工作流、`api-enable` 打开
`enable_api`（environment_publication.py:494-523）。其中已发布工作流在 Dify
里**没有降级路径**——`app.workflow` 一经写入即不可移除。当发布越过中央步之后
被取消或恢复（ADR-0310/0311 的两类死锁修复后才可能走到这一步），`restore`
只回切 env 引用与路由，应用保持提升态——候选被消费但入职未完成。

此后该租户的三条恢复面都被同一份提升态证据封死：

- `consumer-prepare` 对账按 (environment, action, tenant_id) 继承最近一次
  `resources`（environment_tasks.py:338-346），领回的 `expected_app_id`
  指向已提升应用；`environment_operator.run` 的停用不变量（无已发布
  workflow、api-keys 为空）命中即 `ENVIRONMENT_TASK_OBSERVATION_REQUIRED`。
  重跑多少次都领回同一应用，状态机无出口。
- `environment_consumer_trial.observed()` 的候选不变量
  （`enable_api`/`enable_site` 为 False、无 workflow、无 api-keys）对已提升
  应用直接 `candidate entries changed`，业务验证的消费者腿永远阻塞，
  而发布提交又要求一票 observed 的三腿验证——租户永远无法完成入职。
- 即使候选重铸出来，目标侧 `register_runtime` 会按
  `SETUP_PARTIAL_REGISTRATION` 拒绝：第一次登记已写入
  `dify_workspaces`+双端 `operator_apps` 行，新 app_id 查无此行而旧行仍
  占着同租户同端的位子——既不算「全在场」也不算「全缺席」。
- 若取消发生在 `connect` 之后（凭据已回指目标网关），`dependencies` 的
  只读 `verify_credential` 还会以 `SETUP_CONSUMER_PLUGIN_FAILED` 拒绝
  重建。

## 决定

**候选重铸（environment_operator.run）**：领回继承资源并读到候选应用后、
进入既有 verify/停用不变量检查之前，先判「该应用是否已被提升」：判据与
`observed()` 的候选不变量同口径取反——`enable_api`/`enable_site` 不是
False、存在已发布 `workflow`、或 api-keys 非空，任一命中即视为已被发布
消费。命中时清空继承的 `resources` 并重新铸造（新 operation_id、新
`expected_app_id`、新随机名），随后按既有的「应用缺席」路径导入新候选；
旧应用保持提升态原样——它是一次越过中央步的发布的产物，是否已承接过
真实流量不在准备对账的判据范围内，绝不由准备任务代为清理。

提升判定必须落在 verify 之前：已提升即已消费，与草稿是否仍匹配无关——
先比对会把「已消费」误读成「配置漂移」而拒绝，而不是重铸。未提升的继承
应用维持原语义：adopt 后照常 verify，一次幂等导入不重铸。

**凭据回指（environment_consumer.dependencies）**：受管插件凭据恒按候选
形状收敛——不再先只读校验，而是无条件跑 `consumer_plugin` 的幂等
update-or-add（回指 `settings.public_base_url` + 候选令牌），随后
`verify_credential` 仍做单凭据不变量与动态选项断言。connect 之后才被取消
的发布会留下指向目标网关的凭据，只读校验只能拒绝；回指是与
`connect()` 同源收敛目标的逆操作，幂等且在 in-flight 发布下由发布自己的
connect 复核周期再回指，竞态有界。

**登记替换（self_service_store.register_runtime）**：目标侧登记时，若某端
的期望 app_id 无行、但同租户同环境同端已有一行且满足登记血统
（`created_by='environment-runtime'`、revision=1、同工作区、agent_name
恰为 `<entry>-<app_id>` 派生形、同 profile、同 scenario），原位 UPDATE 成
新应用身份并返回「registered」。这只覆盖「同一条登记的候选被消费后换
发」——`chat_route_bindings`/`dify_app_registry` 的既有冲突闸不变（激活
过的发布残留仍以 `SETUP_ROUTE_CONFLICT` 拒绝），observe_only 下不写库、
缺行仍按 `SETUP_PARTIAL_REGISTRATION` 拒绝，非血统行仍
`SETUP_APP_CONFLICT`。

## 替代方案

restore 增加降级路径（api-enable 关闭 + 删除 api-key）：`app.workflow`
无法移除，停用不变量仍不满足，降级到一半仍是死锁——且「曾被提升」本身
就是候选已消费的证据，回滚语义上也不该把租户拉回可验证的假象。

`observed()` 容忍已提升候选：提升态与「第三方动过正式应用」在观察面上
不可区分，放宽候选不变量等于把生产应用漂移放宽成可通过——验证合同被
削弱换不来恢复能力。

发布改为复制出独立正式应用而非原地提升候选：release 的 app 身份、路由
绑定与 `source_configuration` 引用全部以候选 app_id 为准，改成另铸
正式应用是身份模型的重设计，远超本缺陷所需的恢复面；且并不解决
「取消后候选仍可复用验证」这一更小的正确语义，反而把它放大。

由工人手工在控制台删除或降级已提升应用：提升面的操作权限与入口一致性
正是候选合同要管理的内容，绕过受管面修一次现场会把下一次对账建立在
无记录的依赖上。

登记侧放任双行并存（旧 consumer 行 + 新 consumer 行）：`operator_apps`
按 app_id 主键，同租户同端会出现两行，运行时按 (tenant,entry) 的查询
面变得二义；原位替换保持「一租户一端一条」的形状。

## 后果

取消/恢复过的发布会留下一个保持提升态的孤儿应用（含其 api-key）；它不
绑定路由、不进 `enabled` 终态判据，租户侧消费者候选则以全新应用继续
入职。孤儿应用的处置不属于本判据——若需回收，应走独立的受管清理面
而不是准备对账顺手删除（准备任务没有区分「已被承接的正式应用」与
「半死的提升残留」的证据）。

`operator-prepare` 不受影响：`central_bundle` 只提升消费者应用
（`spec["consumer"]["app_id"]`），运营应用的发布载体是 release bundle
里的 manifest/prompt，候选本体不被提升。其余资源步骤
（workspace/knowledge/connect）的继承对账语义不变。

幂等语义不变：重铸发生在「读到了已提升应用」这一确定证据上，与
`attempted` 标记（创建响应丢失的歧义态）无涉；重铸后的新候选仍走
同一套 attempted/对账协议，对账中断可恢复。登记替换发生在同一事务内，
失败回滚与既有语义一致；observe_only 对账读不改变拒绝语义。
