# 管家原生预览已部署，Java 正式路由持续开放

实现合并 `812757dd`（#1122），回执分支 `docs/operator-native-preview-live`。
完成 Gateway CD、控制台桥摘要部署、两条原生预览路由拒绝检查，正式 Java 管家路由恢复
active/revision3/版本22；consumer 两路不变。没有遗留自动撤回 timer。

最终本地4227通过/9跳过，CI4226通过/10跳过；真实Java五组隔离场景和浏览器回放通过。
测试人员正常公司重新登录后，在原生预览发送本轮已授权明确订单；已核同QA completed、run succeeded、
node-41/matched及两项真实CPMS证据，约21秒完成。采样0条为缺证，故障原因未完成业务验收。
用户确认原生面板已显示完整报告，本次真人预览展示通过。
详见[回执](../validation/2026-10-28-operator-preview-deployed.md)。
