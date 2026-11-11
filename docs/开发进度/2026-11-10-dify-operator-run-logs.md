# LOG #1455：Dify 原日志入口的 Java 元数据

目标：按已批准 #1452 规格，在受管管家原 logs 入口显示本人当前有权查看的 Java 运行，
保留其他应用原生日志。本地基线 `3da9be7f`，分支 `feat/dify-operator-run-logs`。

已完成：只读账本查询、固定列表/详情路由、同源 bridge 白名单、Dify 1.17.1 页面补丁与组件，
按 publication_app_id/本人/租户/当前权限指纹过滤；有界分页、元数据白名单、全响应 no-store。
旧运行阶段未采集与业务结果过期明确展示，不恢复答案、不改运行状态、不新增 store。

验证：53 项最终定向检查、95% 新模块覆盖率、2 项授权变异被拦、5 项真实 Chromium 交互、
8 项临时 Gateway 检查及 Python 构建通过；前端静态通过。
扩展回归 344 通过/1 失败，失败用例单独复验通过；Next webpack OOM 未完成。
详细版本、失败与证据见[验收记录](../validation/2026-11-10-dify-operator-run-logs.md)。

下一步：父会话整合 RUN trace 与原生预览应用归属、在资源充足环境完成 web 构建，
另派独立验收并走 CI/PR/人工复核。没有 push、PR、merge 或 deploy；未完成业务验收。
