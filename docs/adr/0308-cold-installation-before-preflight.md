# 308. 真空目标由声明「包含初装」的候选准备先经受管冷装再做现场预检

状态：已采纳；接入真实「未安装」观察的安装面受管动作，不改既有安装事务与租约形状。
原链要求`task.execute`先有`installed.json`，而`candidate-prepare`首步`site-preflight`的回执
又依赖已安装实例的健康事实，真正空机因此无法经页面一键开通：空机预检只能得到阻塞回执。
本决定把冷装做成原安装面的新任务动作`site-install`，置于`site-preflight`之前，
空机事实由执行端在固定路径/单位/端口上实际证明，而不是由表单断言。

## 决定

`site-install`是`environment_installation`平面的子任务：由`candidate-preparation.submit`
在载荷含`initial_installation: true`（页面显式确认「包含初装」，其他取值或偷带均拒绝）时，
作为首个`children`行在父事务内建行；父请求冻结`initial_installation`并计入父编号摘要，
与只读准备提交不是同一任务。子任务沿用同一SQLite行、同一`waiting/executing/checking`状态机、
同一`attempted/dispatched/authorize`授权闸与同一安装面互斥清单（含既有candidate动作）。

`_advance_one`在`initial_installation`下按`site-install→既有STEPS`序列推进：
`site-install`未`observed`前不创建`site-preflight`；确认在场后才经原`tasks.submit`
（`after`绑定本任务编号）延迟建行，使同计划陈旧阻塞预检行不被复用。
冷装结果`observed`仅当`installation∈{installed,already_installed}`；
`not_installed`（空机可证但未装成）只能`blocked`，不推进后续步骤。
stage只导出`application`与`candidate`两制品（无runtime、无expected基线），
`python`小版本取计划部署目标声明；`observe_only`沿用`attempted`语义。

远端`task.py`新增`cold-run`/`cold-observe`：先分类事实——
`installed.json`在场（本任务意图或外来账本）只跳过不重放；
固定`root/state/config/unit`、`-preparing-*`残迹或`systemctl`单位任一在场即`reconcile`恢复原语；
声明`backups`根允许受管staging预建，仅可信私有空目录仍属真空，其中已有内容按旧业务资产对账；
全部缺失且端口可临时绑定才视为真空；意图`cold-intent.json`先于`apply`首写持久在
worker创建的受管staging根（不落在`inst.root`，避免误触安装器「拒收已有根」）；
冷投影只含停用目录与公司连接，不写runtime指针、不装runtime；
事务仍归原`Installer.apply`（pending.json权威、原子接管、试跑健康、切换启用、账本落`installed.json`）。
意图在场而根从未接管、或意图属别任务，均只观察对账不再安装。
既有`installed`路径语义不变：非初装提交不建`site-install`，链路与回执合同原样。

## 替代方案

冷装做成site-plane任务（`{"owner"}`认领）：site面没有stage/authorize/attempted机器，
要把「授权后首写、失联只观察、同目标一租约」重造一遍，且`complete`回执校验形状与预检回执共用一处，
不如复用安装面既有合同。放宽`site-preflight`容忍未安装再让父任务等待：把「是否真空」的判定
推给只读回执措辞，无法表达`reconcile`与`refused`之别，且会让预检在非空机上给出误导通过。
意图写`inst.root/installation-tasks`：安装器冷分支拒绝已存在的根，意图先于apply建根即自相拒收。
worker侧判断空机（HTTP预查）：「未安装」必须在执行端由固定路径与systemd事实取证，
中央侧代查无法区分半失败、路径漂移与未知资产。
`initial_installation`做默认行为：会把「包含初装」藏进普通准备按钮，违反显式确认要求。

## 后果

真正空机可经页面显式确认后一键开通：冷装（停用目录）→原预检→原资源准备链，
`installed.json`落账本后既有candidate-install升级链照旧接管，无重复安装窗口。
页面与`result.steps`显示`site-install`待安装/已装/待核对；取消父任务按安全边界级联冷装子任务。
未知资产、旧单位、占用端口、漂移路径与半失败均落`reconcile`/`blocked`具名结论，不被当作空机重放。
旧计划（installer修订先于本片）在目标上无`cold-*`动作，stage后远端拒绝为未知，
任务停`checking`待新计划，不回溯改写既有修订行为。
