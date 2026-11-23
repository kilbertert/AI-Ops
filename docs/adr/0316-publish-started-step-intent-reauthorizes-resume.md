# 316. 发布步骤 started 授权标记可续跑重授

状态：已采纳；修订 intent one-shot 消费语义，不改取消分支判据（ADR-0310 仍生效）。

`publication-intent` 的 one-shot 门把 `steps.<step>` 的任何存在都判成已消费并返
`{"execute":False,"observed":...}`。但 `{"state":"started"}` 只是授权落账而非执行
结果：worker 死在「intent 已消费、record 未落账」之间时，账本分不清「授权后未跑」
与「副作用已落未 record」。前向重领拿到 execute=False 只能走 observe_only：

- central：intent.json 在而 release.json 不在（死在比对与发行落盘之间）时，未确认
  动作的确认读只能 raise（`Dify publication not confirmed` 等），收敛为
  `ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED`，此后每个认领周期同形永挂；
- target/activate：远端 staged/activated 标记缺席时 observe 同样只能 raise；
- capacity：`capacity outcome unconfirmed; do not replay models` 同形 hold。

任务因此没有设计出口：前向走不出 started；`cancel` 只能置位不能清，取消分支按
ADR-0310 工件标记同样维持核查；BUSY 闸被占死，同环境后续发布提交被互斥拒绝。
env95 实测正中此形：tenant 1783022023241633792 的发布 task_8fe0bcdcf4c11f1a33d8482fe649afa5
readiness/capacity 已 passed、central 只消费 intent（intent.json 已落、release.json 未落），
循环约 70s 报核查，卡片永停 checking。

## 决定

intent 门把 `{"state":"started"}` 从「已消费」改判为「可续跑授权」：重返
`{"execute":True}`（started 原样重写）。依据是各步分发本就按重领可重入构造：

- `central_bundle`：release.json 落盘即走既有 verify 早退；每个非幂等写都有确认读
  守卫（publish 先查已发布 workflow 按同指纹复用、api-enable 先查 enable_api），
  grants/store/capability/service_key 都是 find-or-create，续跑收敛到同一 bundle；
  intent.json 快照只含稳定 spec 字段，重测的 readiness 不参与比对；
- target/activate：远端 publication-task 是标记幂等脚本，标记缺席时重跑即首跑；
- capacity：重测产出的是从未落账的结果——「do not replay」防的是新测量顶替
  已记录结果，started 下没有已记录结果；
- readiness/verify/source/directory/restore：worker 本就不读 execute 返回值，语义不变。

只有 record 落账的结果才把 intent 消费成 `{"execute":False,"observed":...}`。

## 替代方案

worker 侧识别 started 形并直接以 execute 语义调用：授权决定挪出账本门，「步骤是否
可重入」的知识散到 worker，且其他 publication 调用方不受保护，排除。
只对 central 白名单续跑：事故步确实只有 central，但 capacity 死在测量中途、target 死在
authorize 与远端写之间是同根死法；重入性是整条重领模型的既有前提，单列白名单只是
多维护一份清单并留下同类缺陷，排除。
给 steps 增加更细的两段标记区分死在哪一侧：进程死亡即停写，started 之后任何更细
账本标记都同样有盲区；副作用收敛只能由确认读守卫给出，不靠账本自证，排除。
取消分支放松标记 hold：违反 ADR-0310——`phase=restored` 是对「无未观测共享副作用」
的安全断言，中途死亡的中央步副作用集合未知，不能自动给恢复结论，排除。

## 后果

started 形的前向死锁消除：部署后下一个认领周期即续跑收口，env95 在挂任务无需任何
账本改写即走向 activated。capacity started 的代价是一次重复测量（真实模型调用），
换来无人工干预的收敛。已 record 的非 passed 结果仍被 one-shot 消费，测量失败语义
不变。取消分支维持 ADR-0310：started 且工件标记在场的取消仍核查 hold——`cancel`
单向不可清，该 hold 的出口仍是带外人工核查，本次修订既不扩大也不缩小此面。
