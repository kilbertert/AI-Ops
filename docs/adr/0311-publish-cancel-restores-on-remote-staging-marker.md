# 311. 发布取消恢复按远端 staged 标记区分可恢复与未知副作用

状态：已采纳；与 ADR-0310 同族的无出口缺陷，补目标侧恢复判据缺口。

`environment_publication_worker.execute` 的取消分支在「`steps.central` 有 bundle 且
`steps.target` 有意图记录」时无条件发起远端 `target_task restore`。但
`steps.target={"state":"started"}` 只证明 intent 被消费过，分不清它死在远端
task.py 起跑之前（目标侧零副作用）还是 staged 中途（有副作用）。远端 `recover()`
的第一步 `install.trusted(directory)` 对从不存在的
`publication-tasks/<task>` 直接抛 FileNotFoundError——恢复调用本身必崩，任务每
60s 被重领一次并永久占用 BUSY 闸，状态机没有出口。env95 实测出现此形：
任务首周期死在传输 guard（备份目录权限漂移），intent 已消费而远端 task.py 从未
起跑；其后续周期计划过期被拒在 intent；页面恢复后 restore 每轮崩在
`trusted()`，无法终态化。

## 决定

worker 新增 `target_staged()` 探测：经 `dev-host exec` 在目标宿主检查
`<access>/publication-tasks/<task_id>` 是否为目录。该目录是远端 `setup()`
的**首个**落盘写——它不存在，证明钉死版本的远端 task.py 从未进入 staging，
目标侧不可能有已应用的副作用，取消结论与「从未启动」相同：
`state=cancelled, phase=restored`。目录在场则维持原受管恢复：
可能存在未知目标侧副作用，恢复结论必须由 `recover()` 给出。

证据语义按「能给出确定回答」对齐：远端脚本以**退出码 7** 回答「缺席」——
dev-host 自身的传输/权限失败落在 1/255 等普通码上，永远不能被误读成缺席；
探测超时、宿主在 `.dev-host.toml` 不可定位（零或二义匹配）、名称不过引用白名单，
一律视同在场，收敛到保守的受管恢复侧。宿主定位复用 `target_task` 的
name-or-host 匹配规则；access 根取目标 `paths.access`，缺省回退
`/opt/aiops-access-<environment>`。

## 替代方案

让远端 `recover()` 容忍缺目录：安装器按任务钉 sha256（传输校验 + 远端自验），
改它救不了本任务；且「真的恢复了」与「无事可恢复」在 recover 的报告里应当有别，
把缺目录静默成成功会模糊副作用证据的语义。

改探远端 `observe()` 代替裸 `is_dir`：observe 把「缺席」与「过期」合并成同一
"outcome absent or expired" 出口——而「staged 但过期」恰恰是副作用在场的情形；
并且它要先把整条钉死安装器链（构建、传输、自验）跑一遍才能回答一个目录存在性
问题，证据粒度反而更粗。

探测 `/var/backups/aiops-access-<env>/publication-<task>` 传输目录：该目录由
worker 传输 guard 先于 task.py 创建，里面是 installer.tar.gz 与 release.json 等
**惰性输入件**，不算已应用副作用；更实际的反证是——本任务的历次失败 restore
已经把它造出来了，探它等于永久 hold。`publication-tasks/<id>` 才是「远端开始
动作」的最早证据点。

账本侧补记「远端 staging 开始」步骤：与 ADR-0310 同论——intent 账本只写到
`{"state":"started"}` 进程即死，账本无法自证死在哪一侧；文件系统落盘次序是
唯一的外部证据。

## 后果

`steps.target` 只消费了 intent、远端从未留下 staged 目录的任务，取消后按
restored 终态化、释放 BUSY 闸；env95 上楔死的任务在部署后下一个重领周期即
自收口，无需账本改写。目录在场、探测失败或宿主不可定位的任务行为不变，
仍走受管恢复或维持核查 hold。
`/var/backups` 下可能残留惰性传输件（per-task 命名空间、root 所有 0700），
与中央侧 bundle 工件同属证据级留档，不视为需要清理的副作用。
标记的证据力仍是目录存在位：人工删除 `publication-tasks/<id>` 会把「未知
副作用」误判成「可恢复」——与 ADR-0310 同一内生限制，该目录按证据保管。
