# LOG：RUN 集成检查闭环与 1.6.0

分支 `feat/dify-java-run-log`，起点 `557cab62`，RUN main `097aef58` 已合入；PR #1459。
本轮只修已发生的检查问题并准备 1.6.0，不改其他业务。

已确认数据库文件差异来自可复现的 WAL checkpoint，逻辑数据未变；只读验证改为
拒绝写 SQL 并核对完整运行记录，临时同值 UPDATE 变异被拦截。
补实际 record_trace → SQLite → 列表/详情 API 的三状态联动，解决 Devin 提出的证据缺口。
两个 POST 路由补登记，run_id 语法按真实字段入台账，纳管测试替身补 middleware；原断言未放宽。

123 项定向检查通过、1 项既有场景跳过；Ruff、锁文件/依赖一致性与 1.6.0 wheel/sdist 通过。
VERSION/pyproject/包初始化/uv.lock 四处一致，ADR 0242 与运行手册已同步。
细节和证据见[集成验收记录](../validation/2026-11-10-dify-log-run-integration.md)。
提交推送仍沿用 PR #1459；父会话负责远端 CI、评审、合并和部署。未完成真实业务验收。
