# 管家原生预览已部署，Java 正式路由持续开放

实现合并 `812757dd`（#1122），回执分支 `docs/operator-native-preview-live`。
完成 Gateway CD、控制台桥摘要部署、两条原生预览路由拒绝检查，正式 Java 管家路由恢复
active/revision3/版本22；consumer 两路不变。没有遗留自动撤回 timer。

最终本地4227通过/9跳过，CI4226通过/10跳过；真实Java五组隔离场景和浏览器回放通过。
待测试人员使用正常新公司登录，在原生预览发送本轮已授权明确订单后核实际QA/run、双工具与结果。
未完成业务验收，不以“已开放”代替“真人已取数成功”。
详见[回执](../validation/2026-10-28-operator-preview-deployed.md)。
