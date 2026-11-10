# LOG #1455：补 ADR 与单提交交付准备

2026-11-10 UTC；任务分支 `feat/dify-operator-run-logs`，既有 HEAD `94281bbb`，
本地 main 基线 `3da9be7f`。按父任务保留编号：RUN 0240、COMM 0241、LOG 0242。

新增 [ADR 0242](../adr/0242-operator-run-metadata-readback.md)，记录既有 Java 元数据账本、
结果时效分离、当前本人权限刷新、应用归属、字段白名单及只读分页的实际契约；无生产信息。
原验证记录保留，特别是低堆 OOM 及 canonical 参数复跑成功的先后证据，不改写历史。

已核对仓库 ADR 检查按提交判断，而非只看分支最终树。现有 4 个未推送实现提交没有 ADR，
不能仅追加一条文档提交解决门禁；计划在 RUN 合入 main 后，以受控交互 rebase 整理为
包含 ADR 的单个交付提交，再执行 `node .sandcastle/adr.check.mjs --base origin/main`。
操作前将提交历史保存在自己忽略目录的私有 Git bundle，完整变更保存在二进制补丁，
核对整理前后文件树，保留全部文件及验收证据；不 reset --hard，不 force push。

开始本轮时 RUN PR #1457 仍为 OPEN，因此 main 同步、单提交重写及真实 RUN trace 联动待执行；
不在未满足前置条件时引入其他 task 分支。本轮仅文档与交付准备，不重跑已通过的 web 构建。
后续回归只针对受影响日志元数据与合入 main 后的实际 trace；真实整链由父会话执行。
没有 push/merge/deploy；未完成业务验收。

本轮文档检查 25 passed；0242 的状态、备选方案、唯一编号、相对链接及选定敏感信息模式检查通过。
`adr.check.mjs --base origin/main` 实测失败：当前 4 个原提交中 2 个被标为缺 ADR，
这正是等待统一提交要修复的问题，不能把结构报告通过写成逐提交门禁通过。
原历史 bundle 已通过 `git bundle verify`，完整补丁及预重写树哈希已保存在私有备份目录。
证据位于忽略目录 `dist/verification/logs-adr-docs.xml` 和 `logs-adr-before-squash.txt`；
暂存文档未提交，main 同步、历史重写和实际 RUN trace 回归均未执行。
