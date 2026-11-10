# LOG #1455：补齐工具耗时、过期状态与完整构建

分支 `feat/dify-operator-run-logs`，本轮起点 `7c4bcf5a`，main 基线 `3da9be7f`。
目标：按接续反馈补 duration 和在途过期语义，复核授权/preview 接缝，以 canonical 参数完成构建。

已完成：`duration_ms` 元数据白名单及工具耗时 UI；过期 issued/running 只读显示 expired，
终态保留，耗时优先末事件。确认身份接缝已刷新权限，start 已绑定真实 publication_app_id，
无需改父任务负责的 gateway_api/operator_cpms/native_preview。

本轮 35 项 Python/bridge、6 项完整配置 Chromium、前端静态、Ruff、Python 制品通过。
Node 24.20.0 + webpack + 4608 MiB + 单 worker 完整构建成功，解决首轮低堆 OOM；
产物/BUILD_ID/执行命令和边界见[补验记录](../validation/2026-11-10-dify-operator-run-logs-followup.md)。

未引入 RUN task 分支，待其 PR 合并后再集成 origin/main；未 push、PR、merge 或 deploy。
未完成真实/独立业务验收，RUN trace 持久化整合及远端 CI 由后续交付完成。
