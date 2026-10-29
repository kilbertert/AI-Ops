# Thin Harness 清理与目录梳理

目标：删除已无生产依赖的旧代码，明确仍在运行的消费者，给出按职责整理目录的迁移清单。

- canonical checkout：`<canonical-checkout>`，`main`，开始时干净。
- 交付 worktree：`<task-worktree>`。
- 当前分支：`refactor/thin-harness-layout`；基线 `55a284d6`，尚未提交/推送/创建 PR。
- 已核全部 worktree；保留其他任务的未提交研究和策略文档。

## 已完成

删除 `DiagApiSettings`、`Settings.diag_api` 与 `AgentSettings.provider_names()`；
生产代码没有调用，测试已改用现役配置且保留原有行为断言。
新增[目录梳理](../architecture/repository-layout.md)与 [ADR-0157](../adr/0157-remove-unused-diagnostic-config-aliases.md)，
给出旧链调用关系、共享依赖、目标归属、迁移顺序及每片验收。
README 和文档导航增加入口；纠正 tools 说明对正式 Java 节点和模型代理的错误分类。

实测 Python 包根有 129 个模块；Gateway API/运行调度分别为 8,500/3,867 行。
目录目标尚未实施；仍在运行的旧诊断代码没有被冒充成已退役代码删除。

## 验证与交付范围

全量 `4236 passed / 9 skipped`；ruff、compileall、依赖兼容检查、wheel/sdist、
本地验证镜像构建、内容锁和禁网只读容器导入通过。详见[本轮验证](../validation/2026-10-28-thin-harness-layout.md)。
规格检查为 0 错误、53 条既有警告；没有把它写成全部规格已同步。
无新增业务测试：删除的是内部兼容包装，既有配置/数据源/鉴权测试完整保留。

## 缺口与下一步

Thin Harness 仍承接 C端检测、CLI、设备、标准诊断和旧 Dify 试用，
模型/工作区/合同还被问答、分析和发布复用。按迁移清单逐个处理消费者后才能整条移除。
外部 Python 调用方未扫描；若直接引用三个已删除名称，需切换到现役配置。
未完成业务验收，未做远端业务请求、部署或生产退役。
后续目录迁移先归位 Java 节点/代理，再按职责迁 Python 与测试，最后收口专属旧链。

操作知识自检：本轮只做本地 Git、依赖、测试和镜像验证；相关构建命令可在
`deploy/m2-live-03/README.md` 命中。补充可独立复跑的[本地镜像手册](../agents/local-gateway-build.md)，
不记录凭据。没有执行 SSH、SCP、Redis 或服务重启。
