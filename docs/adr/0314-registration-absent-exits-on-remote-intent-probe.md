# 314. 登记 absent 出口按远端意图探针区分零副作用与未知副作用

状态：已采纳；与 ADR-0310/0311 同族的无出口缺陷，补登记腿的恢复判据缺口。

`candidate-register` 任务首派（`register` 远端动作）若死在
`registration-tasks/<task_id>.json` 意图落盘之前——传输瞬断、环境锁被占、
远端 task.py 起跑前崩溃——`dispatched` 已翻转而远端零副作用。此后每个
认领周期只剩 `observe-registration` 可跑，而只读观察无法让未登记的
store 形状变合法（self_service_store 的 PARTIAL/冲突判定按运行时目标
读，与「已派发但未落地」不可调和）；spec 的 `expires_at` 过期后即使能
再派发，远端 `register()` 也会自行降级为观察。任务因此永久停在
`reconcile/unconfirmed` 的 checking 循环：dispatch 的 absent 出口要求
`not dispatched`，complete 的 cancelled 分支排在 reconcile 之后，页面
取消也破不了环，BUSY 闸同时挡住新登记任务的提交。env95 第二租户链实测
出现此形：意图文件缺席、`installed.json` 基线与 registrations.json 租户
全部匹配，登记任务 35 分钟以上在 checking 中无出口。

## 决定

worker 在两种情形下探 `/opt/aiops-access-<environment>/registration-tasks/<task_id>.json`：
远端动作抛出任何 `installation_run` 捕获的异常，或远端正常返回
`result=absent`（store 为空本身不证明登记步零副作用——意图在而
store 空的形状真实存在，须由意图文件缺席收紧成「从未越过首写」）。
该文件是远端 `register()` 在任何 store 写之前的**首个**落盘意图
（`task.py` register 中 `install.write(file, spec)` 先于
`register_runtime`），它不存在且持锁下无远端 task.py 在飞，即证明该次
派发零副作用，任务可安全终态化。

探测契约与 ADR-0311 `target_staged` 完全同形：先非阻塞夺取同一把
`/run/lock/aiops-access-<env>.lock`（O_NOFOLLOW + uid0/0600/nlink1），
持锁后 `is_file` 判意图文件；远端脚本以**退出码 7** 回答「缺席」，0 在
场、9 锁被占或不可信；dev-host 自身失败落在 1/255、探测异常、宿主不可
唯一定位，一律按在场收敛到核查 hold。

worker 证明缺席后把登记结果改写为 `result=absent` 并附
`remote_intent="absent"` 证据键；dispatch complete 的结果校验放宽为
允许该可选键（仅允许缺席值、且仅可与 `result=absent` 同现），absent
出口相应放宽为 `not dispatched or remote_intent=="absent"`：有取消判
cancelled，无取消判 blocked。无证据键的 `absent` 维持原有 dispatched
hold 不变——远端自报的 store-absent 不证明意图文件缺席，二者证据力
不同源。

## 替代方案

允许 `authorize` 对已派发任务二次派发（re-arm attempted/dispatched）：
意图缺席时重跑 `register` 看似更直接，但 spec 内嵌的 `expires_at` 过期
后远端 `register()` 仍按 `observing` 降级——楔死发生时窗口几乎必然已
过，重派修复不了本任务；且放宽派发不变量需要新的远端证明面，收益
不抵复杂度。

放宽 dispatch 对 `result=absent` 的 `not dispatched` 条件而不带证据
键：远端观察回报的 store-absent 只能说明 store 当前为空，证明不了
register() 是否曾越过意图写（意图在而 store 空的形状真实存在——
register_runtime 在意图落盘后死于首写前），把两者等同会让「可能有
副作用」的任务被当成零副作用放行，违背未知不释放互斥的不变量。

让远端 `observe-registration` 对「登记形状部分落地」返回可终态化的
结果：PARTIAL/冲突状态是真实歧形——同租户旧应用行的血统替换语义
只有非观察路径才有权执行，观察面如实拒绝是正确的；缺陷在状态机缺
出口，不在观察分类。

页面侧直接终态化（inspect/cancel 时跳过 worker 证据）：管理面读不到
目标宿主的意图文件，证据只能在 worker 侧产生，与 ADR-0310/0311 的
分工一致。

## 后果

首派死于意图落盘前的登记任务，在部署后下一个认领周期经探针证明缺席
即终态化（blocked，或页面取消后的 cancelled），BUSY 闸释放，新计划
的重交登记经非观察 `register` 走血统原位 UPDATE 完成登记——env95 上
楔死的任务正是此形。意图在场、锁被占、探测失败或宿主不可定位时行为
不变，维持核查 hold；锁被占时下一个重领周期自然复探。
`remote_intent` 证据键随结果留档在任务 result 中，页面可见终态依据。
探针可能在 `/run/lock` 留下空锁文件（root 0600，与 task.py 自建同形），
tmpfs 随重启消失，无害。
标记的证据力与 ADR-0311 同限：人工删除 `registration-tasks/<id>` 会改变
判读（目录按证据保管），残余窗口只剩远端 wrapper 存活但 task.py 尚未
取锁的子秒段，量级在重领周期之外。
