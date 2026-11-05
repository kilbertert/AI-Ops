# Runner队列治理实施

目标：agent长任务不占部署槽，PR过期后任务退出，队列状态可诊断。
分支docs/runner-queue-design；设计已获用户确认。
完成：独立runner注册；CD路由、轻任务分流、PR实时门禁与关闭清理、取消信号与监测。
验证与缺口见[本轮回执](../validation/2026-11-05-runner-queue-implementation.md)。
下一步：独立反例、完整CI、PR评审合并、并发probe与真实CD；长期记录20次真实合并，不造空部署。
