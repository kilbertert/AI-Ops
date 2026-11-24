# 324. 全停用环境即共享运行接线的配置维护窗口

状态：已采纳；平高 95 环境 stage2b `candidate-install` 实测阻塞暴露的窗口
缺失修复。

## 背景

D42（install.md「同环境新增租户保留现役运行」）规定：组合运行快照时，
共享 agent-runs、模型凭据、工具 manifest 及 profile 字节必须与已安装
版本相同，不同则拒绝，「交独立配置维护窗口」。但代码从未实现这个窗口
——`compose_runtime` 无条件拒绝共享差异，`install.py --candidate
--runtime`、`register-runtime`、`publication-*` 也没有任何一条路径能
更新已组合实例的共享成员。

平高 95 环境实测：已安装 `runtime/2` 服务平高+小趋两个停用租户，升级
候选携带 ADR-0322 的 SOP 修订（operator-manifest.json 是唯一差异的共享
成员），候选安装在 `candidate_runtime → compose_runtime` 被拒，升级链
永远停在 OBSERVATION_REQUIRED。

冻结的本意是保护**在用**租户：组合合并逐字节保留其他租户行，若放任
共享成员顺带变化，一个租户的候选操作就静默改写了其他租户的接线。判据
本就不是「操作类型」而是「有没有在用消费者」——同一文件里
`candidate_configuration` 早已用同一判据实现配置维护窗口：目录证明
`enabled` 全为 false（环境行与全部租户行）时接受新配置字节。`apply`
的活动面守卫（861-893 行段）同样只在任一准入打开时执行。

## 决定

`compose_runtime` 增加 `shared_update` 开关；`candidate_runtime` 以现役
目录判据 `enabled`（环境行与全部既有租户行均为 false，键缺失按在用
处理）置位后传入。全停用环境下共享字节允许随候选更新，其余行为不变：
其他租户行仍逐字节保留、替换仍限停用行、谱系摘要（source/previous
runtime sha）照旧记录、recovery 与 pending 事务不变。

任一租户或环境在用、目录缺 enabled 键不可核验、registry 摘要缺失时，
冻结行为与现状逐字相同（fail-closed）。

## 被否方案

- 新增 `runtime-update` 独立动作/任务形态交付共享更新：否决。该窗口
  的受管面（CAS 基线、pending/recover、零在飞、谱系账本）与候选安装
  完全相同，复制一条只换共享成员的路径是纯重复；且本环境全停用时
  「窗口」与「安装」本来就是同一时刻。
- 仅放行「既有租户行替换」、新增租户仍冻结：否决。限制操作类型并不
  比限制环境状态更安全——全停用下 add 与 replace 对在飞消费者同样
  无影响，而规则更碎；冻结真正防的是改写在用接线。
- 在任务请求或页面加显式授权开关：否决。同一提交面内的自我声明不是
  「独立」窗口，只增加了一条可以顺带勾选的旁路；停用判据由目标侧
  现役目录客观证明，比声明更难伪造。
- 直接放宽共享检查不加条件：否决。在用环境上共享变化必须继续拒绝——
  那才是 D42 冻结的本义。

## 后果

- 全停用多租户环境的应用升级可随候选送达 SOP/工具 manifest/模型接线
  修订，不再需要并不存在的单独机制；首个实测用例即平高 95。
- 任一租户在用（含目录行缺 enabled 键的 fail-closed 情形）时共享冻结
  逐字保持；该情形的「独立配置维护窗口」仍是开放的规格债务。
- 共享变化走原 `runtime/2` 组合产物：谱系摘要链完整（source=本次输入
  包，previous=本次 CAS 基线），apply 的停用环境分支本就不复核共享
  字节，无需改动。
- `register-runtime`、preflight、`observe` 语义不变；恢复路径与原
  recover 完全相同（previous 账本恢复旧运行指针）。
- 测试固定：全停用下共享成员变化+同租户行重铸全链 observed、他行
  逐字节保留；缺 enabled 键时共享变化仍拒绝。
