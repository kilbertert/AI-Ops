# 修复管家有效作业被提前终止

基线02d11c3f，分支fix/operator-dispatch-deadline。
将管家同步派发期限对齐已签发run.expires_at，保留默认消费者60/75秒和过期拒绝。
两项回归先红后绿，相关128项通过；全量/远端CI/部署和原H5待完成，未完成业务验收。

最终全量3572 passed / 9既有跳过，ruff/format、compileall/pip check、镜像锁和临时Gateway通过。
