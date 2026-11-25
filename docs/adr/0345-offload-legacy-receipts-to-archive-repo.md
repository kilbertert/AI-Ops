# ADR-0345：将历史验收回执与开发进度归档迁出至独立私有归档仓库并放松冻结门禁

日期：2026-11-25
状态：已采纳
部分替代：[ADR-0097](0097-milestone-records-are-one-file-each.md)（仅替代其原仓库工作树内永久冻结保留 `docs/validation.md`、`docs/开发进度.md` 与历史开发进度的条款；必要验收事件“一事件一独立文件”原则继续保留）
范围：`docs/validation*`、`docs/开发进度*`、`tests/test_milestone_archive_is_frozen.py`、`tests/milestone_archive_headings.json`、`tests/test_repository_layout.py`、`tests/gateway/test_assistant_cancel_handoff.py`、`.sandcastle/conflict-surface.mjs`

## 背景

`docs/validation.md`（1.43 MB）、`docs/开发进度.md`（746 KB）、`docs/开发进度/`（378 文件，556 KB）与 `docs/validation/` 中未再被现役索引引用的 358 份历史回执合计占用近 3.9 MB、738 个工作树文件。虽然 ADR-0097 曾以冻结门禁（`test_milestone_archive_is_frozen.py` + `milestone_archive_headings.json`）防止对旧单文件继续追加，但大量历史过程文本继续留在主开发仓库工作树内，既推高全文搜索与 agent 上下文噪音，也拖慢日常阅读与克隆。

经仓库所有者明确授权：放松冻结门禁，将上述历史回执与开发进度完整迁出到独立私有归档仓库，在主仓库仅保留当前仍被交付状态索引与现役手册引用的活跃回执。

## 决定

1. **先完整迁出并做 SHA-256 校验，再从主仓库工作树移除**：
   - 建立独立私有归档仓库 [`kilbertert/AI-Ops-history-archive`](https://github.com/kilbertert/AI-Ops-history-archive)，从 `kilbertert/AI-Ops@de34dfa5eb6cf48032e11ea1f10419bfedbb880b` 原路径导出全部归档内容并生成 `MANIFEST.sha256` 与 `ARCHIVE_LIST.json`（归档提交 `759e45e85b9731a4b44ac85c5d1a491d829f498e`，含 742 个文件，共 3,982.7 KB，其中 4 份历史联调大文档将在后继 PR 同步退役）。
   - 本 PR 从主仓库移除已归档的 738 个文件：
     - `docs/validation.md`、`docs/开发进度.md`
     - `docs/开发进度/*.md`（全部 378 份，保留说明页 `docs/开发进度/README.md`）
     - `docs/validation/*.md` 中未被任何现役非冻结文档引用的 358 份历史按日回执（保留 110 份现役引用回执、16 份配套结构化证据及说明页 `docs/validation/README.md`）
   - 保持全部 `docs/adr/` 架构决策记录与 `deploy/manual-deploys.md` 留在主仓库，继续接受 `.sandcastle/adr.check.mjs` 与部署台账治理。
2. **同步放松并清理已失效的冻结门禁**：
   - 删除专用于守护两份旧单体文件的 `tests/test_milestone_archive_is_frozen.py` 与 `tests/milestone_archive_headings.json`，同步更新 `tests/test_repository_layout.py` 白名单；
   - 更新 `tests/gateway/test_assistant_cancel_handoff.py` 与 `.sandcastle/conflict-surface.mjs`，去除对已迁出两份旧单体文件与 `docs/开发进度/` 目录的扫描，继续对 `qa-plan.md` 与现役 `docs/validation/` 全量目录执行取消归属与路由契约检查；
   - 更新 `README.md`、`docs/README.md`、`docs/agents/current-delivery-state.md` 与 [ADR-0097](0097-milestone-records-are-one-file-each.md) 的替代标注。

## 备选方案

- **方案 A（连同 110 份现役引用回执一并删除）**：会导致 `docs/agents/current-delivery-state.md` 与现役运行手册出现大量断链，破坏“交付状态可点开核验当前证据”的契约，因此不采用。
- **方案 B（仅保留在 Git 历史而不建独立归档仓库）**：不便于非工程人员或审计方直接按目录检索与独立校验 SHA-256 清单，因此采用独立私有归档仓库 + Git 历史双重可追溯方案。
- **方案 C（本决定：先入私有归档仓并校验 SHA-256，主仓保留 110 份现役引用回执并解除冻结门禁）**：在零损失历史证据、零断链现役入口的前提下，将主仓库工作树文档体积大幅收敛。

## 验证

- 在 `kilbertert/AI-Ops-history-archive@759e45e8` 执行 `sha256sum -c MANIFEST.sha256` 全部 742 项通过并推送远端私有仓库；
- 在主仓库运行全仓现役非冻结文档相对 Markdown 链接扫描，确认零新增断链；
- 运行 `ruff`、`tests/test_readme_architecture.py`、`tests/test_repository_layout.py`、`tests/gateway/test_assistant_cancel_handoff.py`、`tests/tooling/test_doc_impact.py`、`tests/test_ci_test_tier.py` 与 `node .sandcastle/adr.check.mjs --base origin/main`。
