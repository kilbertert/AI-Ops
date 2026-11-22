# 310. 发布取消恢复按工件标记区分可恢复与未知副作用

状态：已采纳；补全恢复判据缺口，不改正常发布/恢复路径语义。

`environment_publication_worker.execute` 的取消分支在「`steps.central` 有意图记录但没有
bundle」时无条件 hold 在 `ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED`——意图是
「不能把未知共享副作用报告为已恢复」。但 `steps.central={"state":"started"}` 只证明
`central_bundle` 被调用过，分不清它死在比对前（零副作用）还是死在授予/登记/密钥/发布
中途（有副作用）；该 hold 在状态机里没有任何出口，任务每 60s 被重领一次并永久占用
BUSY 闸。env95 实测出现此形：selector 缺陷（ADR-0309）使任务死在比对点，恢复请求后
任务无法终态化，阻塞后续验证与发布提交。

## 决定

`describe()` 返回新增 `markers` 兄弟键：`intent`/`release` 为
`<data_home>/publication-bundles/<task>/{intent.json,release.json}` 的存在位。
`intent.json` 是 `central_bundle` 越过比对的**首个**落盘点——它不存在，证明中央步从未
越过「发布源未被改动」判据，此时 grants/store/key/publish 等共享副作用都不可能发生，
取消结论与「从未启动」相同：`state=cancelled, phase=restored`。
任一标记在场则维持原 hold：可能存在未知共享副作用，恢复结论不自动给。
`markers` 放在 `spec` 之外：spec 被 dispatch 逐字比对且喂给 `central_bundle`，
不能混入随文件状态波动的字段。worker 复用每次 execute 已发出的 `describe` 响应，
不新增一次跨端往返；标记由 `describe` 在管理面所在网关本地读取，不向执行端暴露路径。
worker 从运维检出运行、可能先于网关升级：`markers` 键缺失（旧网关不支持）视同标记在场，
维持核查 hold——判据缺席永远收敛到保守侧，不放行恢复。

## 替代方案

在取消分支调 `central`（observe_only）探测 intent.json 存在性：该入口先过 capacity
新鲜度闸（observed_at>900s 即拒绝），探测永远到不了 intent 检查；而且 observe_only
模式下 grants/register/key 等幂等段仍会真实重放——在「要恢复」的分支里重放发布
副作用方向相反，明确排除。
让 `central_bundle` 把进度写进账本 steps 区分死在哪一步：intent 步骤账本只有
`{"state":"started"}`，进程死亡即停写，账本内自证无法区分「死在比对前」与
「死在副作用中途」——工件文件是唯一由落盘次序给出的外部证据。
只查 `intent.json` 不查 `release.json`：release 写毕而 record 前崩溃会留下
「bundle 未记账但副作用已发生」，属于必须留在 hold 侧的情形，所以两个标记任一
在场都 hold。
直接允许人工把该状态改判为 cancelled：绕过的是「未知共享副作用不得报告为已恢复」
这一安全断言本身，恢复结论只能由工件证据给出，不接受手工声明。

## 后果

`steps.central` 有意图记录且工件标记皆无的任务，取消后按 restored 终态化、释放 BUSY 闸；
标记在场的任务行为不变，仍以 reconcile hold 等待人工核查副作用。
env95 上 selector 缺陷留下的死任务（bundle 目录为空属实测证据）在部署后下一个
重领周期即按 restored 收口，无需任何账本改写。
`describe` 返回值多一个键：页面与 spec 比对面均不读它，无兼容影响。
标记 stat 读失败（权限/挂载异常）时 `describe` 回 `markers: null`，worker 视同键缺失维持
核查 hold——失败面收敛到终态化 hold，而不是让任务停在 `executing` 里无限重领。
标记的证据力是文件存在位：人工删除 `publication-bundles/` 会把「未知副作用」判成
「可恢复」，这是以落盘次序为证据这一方案的内生限制——bundle 目录按证据保管，
与账本同级，不接受外部清理。
