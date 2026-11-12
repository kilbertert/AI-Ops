# 开发进度：运行日志行级明细（ADR-0251）

- 日期：2026-11-12
- 分支：`feat/operator-runlog-detail-v2`（worktree `AI-Ops-operator-runlog-detail-v2`，base `96c22ab5`）
- 上一里程碑：#1486（ADR-0249 头部元数据投影）+ #1490（llmdoc 同步）

## 背景

ADR-0249 上线后运营反馈行级内容仍简陋：trace 行只有阶段·状态·耗时。用户确认四层明细
（人话行、入参摘要、结果摘要、派发/复核明细）后实施；`detail` 机器码边界不变，
可展示信息改走 `agent_runs.tool_summaries_json` 新列。

## 内容

- store：`record_tool_summary` 有界写入口（≤8 字段/≤32 字值/≤16 工具）+ `Run.tool_summaries`。
- schema：VERSION 升 2，列入 `LEGACY_COLUMNS`，旧库 ensure 自动补。
- 发射：`TOOL_SUMMARY_FIELDS` 11 工具字段白名单；成功路径摘要 `data`，拒绝路径摘要
  `入参+拒绝码`；dispatch `tools=N`、复核通过 `allowed`。
- 投影：`tool_summaries` 进 detail（仍同 scope 闸）。
- 前端：整句渲染（第 N 次调用/派发携带 N 工具/复核结论）、行下摘要、11 工具中文名、
  `tools=N` 复合码。

## 评审与 Hack 复核

- Devin 初审 + 独立 Hack 同时抓到 🔴：v1 库启动阻断（`_version` 只认 0/2）——
  已修并加 v1 迁移测试；另修摘要 `TypeError` 逃逸、manifest `AttributeError`、
  摘要归属历史事件、工具行累计耗时丢失；run-logs.md 边界同步。
- 已 resolve 全部 4 条 Devin 线程，重触发复审。

## 已知缺口

- 「真·逐轮模型决策」未做（需 Java 节点上报协议变更）；本轮按 tool 事件序号推演轮次。
- 非白名单内工具的行级摘要为空属预期；新增工具需同步 `TOOL_SUMMARY_FIELDS`。
- vitest 浏览器用例未在本机执行（依赖 web 构建环境）。

## 下一步

- 独立 Hack（重点：摘要越界泄漏、复合 detail 解析、迁移后旧库读回）。
- 合并后 41 CD + 36 web 镜像双端部署，再补部署验收记录。
