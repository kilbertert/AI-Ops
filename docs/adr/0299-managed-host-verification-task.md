# 299. 受管主机身份验收沿原子任务领取由受管执行端取证

状态：已采纳；接续P4规划与D22现场回执三项pending合同。本片只交付`managed-host-identity`一项的取证任务，
正式网络/业务验收继续各自的受限执行器，不登记、不运行模型、不激活。

## 决定

`managed-host-identity`是回执合同留给跨主机执行器的检查，本机就绪观察永远报告pending。
新增`host-verify`任务动作：页面在有效冻结计划下提交，按`plan digest + action`去重复用编号；
任务进入environment_tasks并沿用原子任务的queued/running/reconcile与lease/取消/漂移合同，
由开发宿主既有受管timer（environment-task-worker）领取。

执行端按任务target在`.dev-host.toml`唯一解析声明名，dev-host在接通前已断言SSH指纹；
实际访问在目标只读采集`hostname`与`/etc/machine-id`的SHA256和服务身份存在性。
回执scope=`managed-host-verification-only`，`check=managed-host-identity`，状态observed/blocked，
证据字段只含声明名、解析目标名、远端hostname、machine-id摘要与目标服务身份是否存在；
created/expires沿用900秒窗口，换电脑或重复提交按同编号对账，进程失联按既有lease恢复。

连接层失败（不可达/断言失败）仍走`ENVIRONMENT_TASK_TARGET_UNCONFIRMED`，不把「没有连上」
当作「验过不通过」；连通后字段缺失或声明解析歧义才写blocked。计划/目录漂移沿`_current`拒绝。

## 被否决的替代形态

把身份证据塞进site-preflight结果会模糊本机就绪与跨主机验收两类事实，且不能独立复验；
新建一张验收表为一个检查重复租约、取消与恢复合同；
一次任务同时取三项pending会让数据阻塞的业务检查拖住可立即完成的主机身份取证；
不采machine-id而只信SSH指纹无法区分「同一主机」与「同密钥换了机器」。

## 后果

`host-verify`只证明「任务实际落在已登记且指纹断言的主机上」，
不证明正式网络、容量或业务能力，计划级四项pending与回执两项pending不变；
activation判断不得引用单个验收任务通过代替完整身份/网络/业务证据。
首次冷装主链、正式网络与业务取证的受限执行器、容量测量均为后继增量；
真实`dev-host`指纹断言在替身测试中不可证明，按既有worker合同现场另验。
