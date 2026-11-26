# 348. main 集成与版本发布分开

状态：已采纳；替代ADR0112的逐提交部署备注方式，更新ADR0233的发布触发与监控部分。

## 决定

短任务分支、PR、CI继续合入可部署的main；普通合并不触发生产CD、不要求改版本号。
负责人准备发布批次时，通过普通PR同步VERSION、pyproject.toml、运行时版本及uv.lock，
再以main为workflow ref，给CD传入已合入main的完整target_sha。所有版本、源码和发布说明以该SHA为准。

CD先验证SHA的完整性、main祖先关系与SemVer；在现有部署锁内创建或核验同SHA的注释版本标签，
再检出该SHA运行已有部署脚本。轻量标签、同版本异SHA及异SHA竞争在部署前拒绝，不移动旧标签。
标签保留发布候选的归属，不证明已经部署：失败后可重跑相同SHA，代码修复需新版本。
仅在部署成功后生成CHANGELOG中该版本的章节并发布同tag的GitHub Release；说明失败只重跑说明作业。
CHANGELOG仍是gitignored生成物，不回写main；旧release-<sha>记录与既有标签保留。

CD Watch保留workflow结果与deploy-41结果：发布说明失败不再冒充完整成功，也不被解释为需要回滚服务。
无发布任务时不检查空闲runner；取数失败、已启动任务卡住与连续失败仍按既有阈值告警。

## 替代方案

- 常驻develop能隔离一条开发线，但不能独立控制main合并与生产发布的频率，额外引入修复回合成本；不采用。
- 保留合并自动部署、只延后Release，能少生成记录，但生产仍每次重启；不能满足批量发布需求。
- 部署后才抢版本标签，写权限更窄，但竞争冲突可能在生产更新之后才发现；采用部署前固定标签。
- 全新发布工具或多环境发布平台可增加功能，但现有workflow、部署脚本及dev-changelog已够用；不新增平台。

## 后果与依据

main可积累多个已评审增量，生产版本由负责人明确选定；任务合并与工作树退休不等于发布。
未完成功能的关闭路径、共享数据兼容、组件部署顺序及恢复要求不变；临时集成分支仍须另行评审。
生产落后main时，紧急修复遵循服务器的制品恢复或维护线例外，不能偷偷把全部待发布变更一起送上去。

workflow整体串行并保留旧cd-41-deploy锁以兼容切换时尚未结束的旧部署；一次只发起一个版本，
不把Actions默认单pending规则当FIFO队列。版本标签一经保留不回收，可能存在没有成功部署的候选标签；
发布成功以run、Release及现役health为准，不能只看tag。

平台映射：[workflow_dispatch输入与ref](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)、
[并发组](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)、
[注释标签对象与引用](https://docs.github.com/en/rest/git/tags)、[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html)。

验收复用根acceptance.feature的版本发布规则、qa-plan.md的RELEASE-BATCH用例与现有pytest/Node自检；
临时Git远端和CLI替身验证失败边界，不访问生产，也不声称业务验收完成。
