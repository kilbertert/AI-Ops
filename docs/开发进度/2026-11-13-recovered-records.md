# 退役工作树未提交文档回收入库

日期 2026-11-13，分支 docs/recovered-records，基线 35a21b70。

任务背景：批量清理 `.worktrees/` 任务工作树时发现 8 个脏工作树持有从未提交的文档，
另有 10 条本地分支携带未入库记录。全部内容先整树归档至私有位置，再由用户逐项裁定。
本 PR 只收录用户点名单保留的部分，按其原仓库相对路径归位，遵循现行目录规则。

## 收录清单

- `docs/agents/claude-mem-runbook.md`（原 `chore/claude-mem-pilot` 工作树未提交；
  claude-mem 服务仍在运行，此为唯一运维手册）
- `docs/validation/2026-11-05-claude-mem-pilot.md`、`docs/开发进度/2026-11-05-claude-mem-pilot.md`（配套记录）
- `docs/agents/consumer-wf01/tax-source-investigation.md`（商城税费只读取证 SOP）
- `docs/validation/2026-11-11-mall-tax-origin.md`、`docs/开发进度/2026-11-11-mall-tax-origin.md`
  （实质结论：税费由商城计算、充电三字段与商城逐值一致，此前未入库）
- `docs/agents/operator-runtime-options-research.md`（原 `docs/research/2026-10-25-operator-runtime-options.md`；
  `docs/research/` 非现行目录，按 agents/*-research.md 惯例改名并修订两处互链）
- `docs/validation/2026-10-25-operator-business-research.md`、`docs/开发进度/2026-10-25-operator-business-research.md`

## 未收录（用户裁定删除或留档不入库）

- 共享上下文选型调研、#1448 schema 计划草案、管家架构复核草案（已由 #1372 交付取代）、
  两份工作树补丁注记：已从归档删除。
- 管家架构复核的 validation/开发进度两篇附件：其唯一主题文档被裁定删除，
  单独入库会造成悬空引用，留档于私有归档未入库。
- 10 条本地分支的孤本内容（#573 重构、双入口复核文档、m2-live07 记录等）：
  分支已删除，完整补丁与逐文件副本在私有归档 `~/.cache/aiops-worktree-archive-20261113/branch-*`。

## 边界

收录文档为原文入库，除 `operator-runtime-options` 改名导致的互链修订外未改正文；
其引用的 2026-10-25 基线源码路径部分已随运行时退役失效，属历史记录正常状态。
本 PR 不含任何业务/源码改动，未完成业务验收不适用。
